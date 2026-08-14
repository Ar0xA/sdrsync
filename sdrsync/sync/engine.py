"""One-directional sync engine: transceiver (rigctld or flrig) -> WebSDR,
and nothing in the other direction.

The rig connection and the WebSDR connection are deliberately independent
subsystems with independent lifecycles -- picking a different WebSDR has
nothing to do with the transceiver, and (re)connecting the transceiver has
nothing to do with which WebSDR is loaded. A single SyncEngine instance
owns both, for the whole lifetime of the GUI session (created once, when
the app starts; torn down once, when the app window closes) -- individual
"Connect" actions from the GUI only start/stop one subsystem at a time via
the thread-safe *_from_other_thread() methods below, never the whole
engine.

The WebSDR subsystem's browser lifecycle is owned by a WebViewHost
(sdrsync/gui/webview_host.py) supplied by the GUI layer -- SyncEngine
itself only knows the narrow create_page()/destroy_page() interface below,
not wx, so engine-level tests can supply a plain stub instead of a real
wx.App. See the v5 migration plan for why: Playwright's out-of-process
Chromium (driven via CDP) was replaced with an embedded wx.html2.WebView,
which must be created/destroyed on the GUI thread -- the host is what
does that dispatch.

Publishes status snapshots for the GUI over a plain queue.Queue (this runs
in a background thread; the GUI polls the queue from the wx main-loop via
a timer).
"""
from __future__ import annotations

import asyncio
import logging
import queue
import random
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Optional, Protocol

from sdrsync.config import KNOWN_SITES, AppSettings, WebSDRSite
from sdrsync.gui_messages import GuiMessage
from sdrsync.preflight import check_websdr_url
from sdrsync.rig.base import RigState
from sdrsync.rig.fake_flrig import FakeFlrigState
from sdrsync.rig.fake_flrig import start_server as start_mock_flrig
from sdrsync.rig.fake_rigctld import FakeRigState
from sdrsync.rig.fake_rigctld import start_server as start_mock_rigctld
from sdrsync.rig.flrig import FlrigClient
from sdrsync.rig.rigctld import RigctldClient
from sdrsync.websdr.base import WebSDRIncompatibleError, WebSDRStatus
from sdrsync.websdr.browser_shim import BrowserError as PlaywrightError
from sdrsync.websdr.browser_shim import PageLike as Page
from sdrsync.websdr.registry import DRIVERS

logger = logging.getLogger("sdrsync.engine")

DEFAULT_POLL_INTERVAL_S = 0.2  # see AppSettings.poll_interval_s -- now user-configurable, this is just the fallback
FREQ_DEBOUNCE_S = 0.2          # candidate frequency must be stable this long before pushing
FREQ_CHANGE_THRESHOLD_HZ = 0  # ignore rig jitter smaller than this
# Was 10 Hz, then 1 Hz, until live testing with a real rig: rigctld/flrig
# frequency readbacks are exact digital values (no analog noise), not a
# measurement subject to real jitter, so any nonzero threshold ends up
# blocking some genuine step size sooner or later -- 10 Hz blocked a
# 9-10 Hz CW/RTTY spotting step, and 1 Hz (the first fix) still blocked a
# rig's own finest tuning-step button (confirmed live: many rigs offer an
# exact 1 Hz step, which never exceeded a threshold of 1). 0 means "any
# actual change is real" -- a reading that hasn't changed at all still
# compares equal and is correctly ignored, and FREQ_DEBOUNCE_S below
# already does the real work of requiring a reading to be stable before
# it's pushed, so there is nothing left for a nonzero threshold to usefully
# filter here.
# Belt-and-suspenders: forces a forward re-push of the rig's current
# mode/freq onto the WebSDR every this-often, REGARDLESS of whether
# _last_sent_mode_key/_last_sent_freq already look like a match. Purely
# a safety net against a live-reported desync (WebSDR page and the
# real rig's own reported state persistently disagreeing, silently, no
# error shown) that couldn't be pinned to one exact root cause -- the
# forward/reverse dedupe latches only re-push when THEY notice a change,
# so any bug that leaves one of them stale forever (this codebase has
# already had two related bugs in that family this session) leaves the
# two sides disagreeing indefinitely with nothing to self-correct. A
# redundant re-push of an already-correct value is a harmless no-op
# elsewhere in this codebase; this trades a small periodic cost for
# guaranteeing the desync can never persist longer than this interval.
FULL_RESYNC_INTERVAL_S = 30.0
# v14 (be-a-good-network-citizen): global floor on how often ANY forward
# push may actually write into the WebSDR page, measured from the last
# write -- shared across BOTH axes (mode and frequency) and the PTT-edge
# mute/unmute. Every one of those calls sends a real command down the
# page's own WebSocket to someone else's volunteer-run receiver, and
# nothing previously bounded that rate: at the minimum poll interval
# (MIN_POLL_INTERVAL_S = 0.05) a rig whose frequency is being spun
# continuously produces a write every 50ms, indefinitely, unattended.
# Deliberately mirrors REVERSE_MIN_RIG_WRITE_GAP_S's shape (one
# timestamp, stamped unconditionally after any write attempt; one boolean
# computed once per tick and applied uniformly to every axis) -- a
# per-axis floor would not be a global gate, and a mode+freq pair is one
# logical retune that should pass or wait together.
WEBSDR_MIN_WRITE_GAP_S = 0.5
# Per-axis failure ladder for a forward push the driver reported as not
# applied (page call failed, frequency outside every band the receiver
# covers, unmapped mode, ...). Without it, a permanently-failing push is
# retried at the full poll rate forever -- and every retry is another
# command at the far end. 1s, 2s, 4s, 8s, 16s, capped at 30s -- the same
# doubling shape as the attach ladder below.
FORWARD_PUSH_BACKOFF_BASE_S = 1.0
FORWARD_PUSH_BACKOFF_MAX_S = 30.0
# A genuinely NEW target value (the rig actually moved) grants one
# immediate retry past the current backoff -- but only while the ladder
# is still shallow. Past this many consecutive failures a new target
# waits its turn like anything else, closing the hole where a rig being
# spun continuously would present a "new" target every tick and thereby
# evade the ladder indefinitely.
FORWARD_PUSH_IMMEDIATE_RETRY_MAX_FAILURES = 3
# Cap on the EXPONENT itself in both doubling ladders below (attach and
# forward-push). The results are already clamped to their respective
# MAX delays, so this changes no observable timing whatsoever: 2**16 is
# ~65k x either base, far past both ceilings. It exists because this app
# is explicitly meant to run unattended indefinitely, and against a site
# that is simply gone the failure count grows without bound -- computing
# 2**50000 on every retry to then immediately throw it away is pure
# waste. Chosen well above the rung where either ladder pins to its
# ceiling, so it can never become a de-facto second cap.
BACKOFF_EXPONENT_CAP = 16
ATTACH_RETRY_BASE_DELAY_S = 2.0
# Ceiling raised from 30s to 5 minutes (v14): a WebSDR that has been
# refusing to attach for several minutes is down, moved, or has stopped
# accepting us -- and a full attach() is a complete page load against a
# volunteer's server, not a cheap ping. Retrying one of those every 30s
# forever, unattended, is the behavior this round exists to stop.
ATTACH_RETRY_MAX_DELAY_S = 300.0
# Multiplicative jitter applied to every ladder delay, so a fleet of
# clients that all lost the same receiver at the same moment (the
# receiver itself restarting, which is exactly when it is most fragile)
# doesn't come back as a synchronized thundering herd.
ATTACH_RETRY_JITTER = (0.8, 1.2)
# How long an attachment must stay up before the failure ladder is
# considered "recovered" and reset to zero. Without this, a site that
# accepts an attach and drops it seconds later (a flap -- the shape a
# server-side fair-use kick actually takes) resets the ladder on every
# cycle, so the backoff never grows and the client hammers the site
# forever at the base delay.
ATTACH_STABLE_S = 30.0
# Once the ladder is this deep, run preflight's cheap single HTTP GET
# before spending a full page load on another attach attempt.
ATTACH_PREFLIGHT_GATE_FAILURES = 3
# Once the ladder is this deep, surface a non-fatal "still retrying"
# status. 5 consecutive failures means ~30s+ of accumulated retrying
# (2+4+8+16, plus each attempt's own duration) and a next wait of ~32s
# that only grows from there -- the point where a user watching
# "reconnecting..." deserves to be told it is still trying rather than
# left guessing whether the app gave up.
ATTACH_STILL_TRYING_FAILURES = 5
ATTACH_CHECK_INTERVAL_S = 1.0  # how often to notice the driver dropped attachment
# Minimum gap between retries of a FAILED idle auto-resume -- see
# _idle_resume_retry_at. Not a ladder: unlike the attach path, a failure
# here means the session never started at all (no driver for the site's
# type, WebView creation failing), which is a local problem, not load on
# someone else's receiver, and it typically needs a human anyway.
IDLE_RESUME_RETRY_GAP_S = 30.0
# How long to keep retrying an initial rig connection before giving up and
# surfacing a real error instead of leaving the GUI stuck at "connecting..."
# forever. Applies to the time-to-FIRST-connect only -- cleared once
# ensure_connected() succeeds once, so a later transient drop-and-reconnect
# isn't bound by this same budget (see _tick()/_start_rig()).
RIG_CONNECT_TIMEOUT_S = 10.0

# v11 reverse sync (WebSDR -> rig) constants.
# Hold-off window, from either direction's last successful push, during
# which the OTHER direction's push consideration is fully skipped --
# comfortably exceeds websdr_org.py's own documented worst-case
# FREQ_VERIFY_DELAY_S = 0.6, so a stale post-forward-push page read isn't
# mistaken for a user edit (see the v11 plan's Blocker-1 fix), and
# suppresses a forward re-push flickering back to a stale rig read
# immediately after a reverse push (Blocker-4 fix).
REVERSE_HOLDOFF_S = 1.0
# Longer than the forward direction's FREQ_DEBOUNCE_S -- this filters a
# continuous human drag/click gesture on the page, not rig hardware
# jitter, which would otherwise hammer set_freq() many times per second.
REVERSE_FREQ_DEBOUNCE_S = 0.5
# Was 50 Hz ("coarser than the forward direction's FREQ_CHANGE_THRESHOLD_HZ
# -- a waterfall click readback isn't pixel-exact") until a live user
# report: that reasoning only fits a mouse-drag/click on the waterfall,
# where the landed frequency genuinely isn't exact -- it doesn't fit a
# discrete, EXACT step button (e.g. a page's own "+10 Hz" control), and
# this engine has no way to tell which UI element produced an observed
# change, only the resulting value. With a strict '>' comparison used to
# decide BOTH "is this a new candidate" and "is it different enough to
# push" (same bug shape FREQ_CHANGE_THRESHOLD_HZ had on the forward side,
# see that constant's own history), a run of +10 Hz clicks never
# individually exceeded 50 -- so the candidate never updated at all,
# every click just accumulated invisibly, until enough of them finally
# pushed the total past 50, at which point the whole backlog fired at
# once. That is exactly "nothing happens for several clicks, then it
# suddenly jumps", confirmed live. REVERSE_FREQ_DEBOUNCE_S below already
# does the real job of absorbing an in-progress drag/click gesture (it
# requires the observed value to stop changing before pushing), so this
# only needs to absorb genuine read noise, not gate legitimate small
# changes -- 0 means only a truly unchanged reading is ignored, same
# reasoning as the forward-direction fix.
REVERSE_FREQ_CHANGE_THRESHOLD_HZ = 0
# Debounces a discrete mode click/button the same way REVERSE_FREQ_DEBOUNCE_S
# debounces a continuous drag -- shorter than the freq value since a mode
# click is terminal (no "still moving" case to wait out), this only exists
# to swallow a double-click or an immediate self-correction, not to add
# latency to the common single-click case. NOT what fixes rapid clicking
# through several modes in a row -- that's handled by the retry ladder's
# target-supersede logic below (a stale in-flight push gets dropped, not
# chased), so don't "fix" perceived chasing by raising this value.
REVERSE_MODE_DEBOUNCE_S = 0.35
# Retry ladder for a reverse push (mode or frequency) that rigctld/flrig's
# own readback verification didn't confirm on the first try -- real CAT-bus
# hardware can be slower than any single verification window budgets for,
# especially right after the user rapidly changed modes/frequency more than
# once. Each attempt gets its own short verify window (passed to
# RigClient.set_mode()/set_freq() as verify_budget_s, overriding their
# module-level default), with a small pause between attempts.
#
# Live evidence (real hardware) showed the original 0.5s/3-attempt shape
# gave up too early under CAT-bus congestion ("rigctld accepted M CW 0
# but rig never confirmed it within 0.5s"), producing false "rejected"
# reverts of a push that was actually still landing. Fewer, longer
# attempts is the right fix, not more/faster polling within the same
# short window: every verify readback is itself a CAT-bus transaction
# that competes with the very SET command it's waiting on, so a shorter
# budget with tighter polling makes congestion worse, not better.
# REVERSE_PUSH_MAX_ATTEMPTS dropped from 3 to 2 for the same reason
# rigctld.py's own docstring already argues against resending while a
# command may still be landing -- three attempts on an already-congested
# bus is three chances to collide with a command still in flight, not
# three independent tries.
#
# Nominal worst-case time to give up: REVERSE_MODE_DEBOUNCE_S (or
# REVERSE_FREQ_DEBOUNCE_S) + REVERSE_PUSH_MAX_ATTEMPTS *
# REVERSE_PUSH_ATTEMPT_VERIFY_S + sum(REVERSE_PUSH_BACKOFF_S)
# ~= 0.35 + 2*1.0 + 0.35 = 2.7s (mode) / 0.5 + 2*1.0 + 0.35 = 2.85s
# (freq) -- and that's before accounting for a poll that itself times
# out overrunning an attempt's own budget by up to one cmd_timeout,
# plus (rigctld only) up to one connect_timeout if that poll's timeout
# closed the connection and _reconnect_if_dropped() has to rebuild it --
# a fully pathological attempt is therefore ~1.0 (SET) + 1.0 (budget) +
# 0.25 + 1.0 (poll) + 2.0 (reconnect) ~= 5.25s, so ~11s for the whole
# two-attempt ladder. That needs a blackholing host, not a merely slow
# rig; the ~3s figure is the realistic case, not a bound. And
# REVERSE_MIN_RIG_WRITE_GAP_S can add a further, usually-overlapping
# delay on top. Treat this as "roughly 3s", not a precise bound.
REVERSE_PUSH_ATTEMPT_VERIFY_S = 1.0
REVERSE_PUSH_MAX_ATTEMPTS = 2
REVERSE_PUSH_BACKOFF_S = (0.35,)  # gap after attempt 1; matches REVERSE_MIN_RIG_WRITE_GAP_S
# Global floor on how often ANY reverse push may actually write to the
# rig (set_mode()/set_freq()), measured from the end of the previous
# write -- shared across BOTH axes and across superseded ladders. Neither
# the per-axis debounce above nor the retry ladder's target-supersede
# (dropping a stale in-flight ladder when the user clicks past it) bounds
# the real CAT write rate: supersede abandons the *ladder* bookkeeping,
# but the command it already wrote is still landing on the bus, and
# nothing previously stopped the next value's push from firing on the
# very next ~0.2s tick right behind it. A single CW mode click alone can
# trigger both a mode AND a frequency write (CW offset) on two
# independent per-axis clocks -- this floor is what actually caps "how
# often do we hit the rig", not either axis's own debounce.
REVERSE_MIN_RIG_WRITE_GAP_S = 0.35

# AppSettings.force_ssb_to_data_mode: per-backend rig-native mode token to
# send instead of plain "USB"/"LSB" on a reverse-sync push. Two different,
# genuinely correct conventions for the SAME concept (data-over-SSB, keyed
# via the rig's data input rather than the mic), not a guess at one:
#   - rigctld/hamlib: "PKTUSB"/"PKTLSB" are hamlib's own canonical
#     protocol-level mode tokens (confirmed via `rigctl`'s real man page,
#     the 'M'/set_mode entry's token list -- backend-independent, hamlib
#     normalizes whatever the specific rig model calls it internally into
#     these). Live-confirmed by the user against a real Yaesu rig: set to
#     "DATA-U" on the rig itself, rigctld reports the mode back as
#     "PKTUSB" -- i.e. this is genuinely what comes back over the wire,
#     not just what the docs say to send.
#   - flrig: "DATA-U"/"DATA-L" match this codebase's own existing forward-
#     direction _MODE_MAP entries in kiwisdr.py/websdr_org.py/
#     openwebrx.py (present before this feature; not a new guess), but
#     NOT independently live-verified against a real flrig instance this
#     session -- flrig's mode-string tables are per-rig-model, so this
#     may not be the exact token every flrig-supported rig uses.
_REVERSE_SYNC_DATA_MODE = {
    "rigctld": {"USB": "PKTUSB", "LSB": "PKTLSB"},
    "flrig": {"USB": "DATA-U", "LSB": "DATA-L"},
}


def _reverse_sync_data_mode_for(hamlib_mode: str, rig_backend: Optional[str]) -> str:
    """Maps a canonical "USB"/"LSB" to that backend's own data-mode token
    when AppSettings.force_ssb_to_data_mode is on. Any other mode (CW,
    AM, FM, ...) or an unrecognized backend passes through unchanged --
    this is deliberately narrow (SSB only), not a general mode
    substitution mechanism."""
    per_backend = _REVERSE_SYNC_DATA_MODE.get(rig_backend or "")
    if per_backend is None:
        return hamlib_mode
    return per_backend.get(hamlib_mode, hamlib_mode)


class WebViewHost(Protocol):
    """The narrow interface SyncEngine needs from whatever manages the
    actual browser widget -- deliberately NOT importing wx or
    gui/webview_host.py here, so this module (and tests against it) never
    need a real wx.App. gui/webview_host.py's WebViewHost class satisfies
    this structurally; engine-level tests can supply any stub that does."""

    async def create_page(
        self, loop: "asyncio.AbstractEventLoop", on_dead: Optional[Callable[[str], None]] = None
    ) -> Page: ...

    async def destroy_page(self, page: Page, loop: "asyncio.AbstractEventLoop") -> None: ...


class RigClient(Protocol):
    """The narrow interface SyncEngine needs from either rig backend.
    ensure_connected/get_state/close are used by the existing forward
    (rig -> WebSDR) sync in _tick()/_stop_rig(); set_freq/set_mode
    additionally support the reverse direction (WebSDR -> rig, v11).
    reconnect_delay() exists on both RigctldClient and FlrigClient but has
    no call site here for either, so it's intentionally excluded."""

    async def ensure_connected(self) -> bool: ...

    async def get_state(self) -> RigState: ...

    async def close(self) -> None: ...

    async def set_freq(self, freq_hz: int, verify_budget_s: Optional[float] = None) -> bool: ...

    # Mode-only, deliberately -- neither backend implementation touches
    # filter/bandwidth on a reverse-sync mode push (see RigctldClient/
    # FlrigClient.set_mode()'s own docstrings for why: the WebSDR page
    # can't reliably say which specific width it wants, and a user
    # reported live that any reverse-sync filter change was unwelcome
    # even when a "sensible" value was being sent).
    async def set_mode(self, mode_name: str, verify_budget_s: Optional[float] = None) -> bool: ...


@dataclass
class _ReversePush:
    """Tracks one in-flight (or backing-off-between-attempts) reverse-sync
    retry ladder for a single axis (mode OR frequency) across ticks.
    target is the value being pushed (str hamlib mode name, or int Hz)."""
    target: object
    attempts: int = 0
    next_attempt_at: float = 0.0


@dataclass
class _ForwardPushBackoff:
    """Failure ladder for ONE forward-push axis (mode OR frequency).

    Deliberately NOT _ReversePush, despite the similar shape: that class's
    target/attempts fields carry reverse-sync-specific meaning (supersede
    a stale in-flight ladder, a bounded verify budget, and a give-up that
    reverts the page to the rig) which forward pushes have none of. This
    project has already conflated two different meanings under one name
    once and had to unpick it; keep them separate.

    last_target exists only to recognize "the rig actually moved to
    something new" for the one-immediate-retry grant -- it is the value
    last *attempted*, not a dedupe latch (_last_sent_mode_key/
    _last_sent_freq remain the only "is the WebSDR up to date" answer)."""
    failures: int = 0
    next_attempt_at: float = 0.0
    last_target: object = None

    def note_target(self, target: object) -> None:
        """Called each tick a push for this axis is being considered."""
        if target != self.last_target:
            self.last_target = target
            if self.failures < FORWARD_PUSH_IMMEDIATE_RETRY_MAX_FAILURES:
                # A genuinely new value is worth one prompt try -- but see
                # FORWARD_PUSH_IMMEDIATE_RETRY_MAX_FAILURES for why that
                # grant stops once the ladder is deep.
                self.next_attempt_at = 0.0

    def record_failure(self) -> None:
        self.failures += 1
        self.next_attempt_at = time.monotonic() + min(
            FORWARD_PUSH_BACKOFF_BASE_S * (2 ** min(self.failures - 1, BACKOFF_EXPONENT_CAP)),
            FORWARD_PUSH_BACKOFF_MAX_S,
        )

    def record_success(self) -> None:
        self.failures = 0
        self.next_attempt_at = 0.0

    def reset(self) -> None:
        self.failures = 0
        self.next_attempt_at = 0.0
        self.last_target = None


@dataclass
class StatusSnapshot(GuiMessage):
    # "_active" means "a session has been requested/started" (may still be
    # connecting/attaching); "_connected"/`websdr` presence means "actually
    # up right now". The GUI needs both to tell "connecting..." apart from
    # "not connected because never started" apart from "lost connection".
    rig_active: bool = False
    rig_connected: bool = False
    rig_freq_hz: Optional[int] = None
    rig_mode: Optional[str] = None
    rig_ptt: Optional[bool] = None
    rig_error: Optional[str] = None
    # GUI rewrite: seconds left before the engine gives up on the current
    # connect attempt (see RIG_CONNECT_TIMEOUT_S / _rig_connect_deadline)
    # -- None once connected or when no attempt is in flight. Lets the
    # status bar show a live "trying to connect (Ns)" countdown instead
    # of silently sitting on the same text for the whole attempt, which
    # was confirmed live as part of why a failed connect attempt read as
    # the app doing nothing.
    rig_connect_remaining_s: Optional[float] = None
    # Mirrors SyncEngine._rig_generation -- lets the GUI recognize a
    # snapshot published by a rig connect attempt from BEFORE the most
    # recent Connect click (queue backlog) and skip it for one-shot
    # popup purposes, rather than misreading it as this attempt's result.
    # See that field's own docstring for the race this closes.
    rig_generation: int = 0
    websdr_active: bool = False
    websdr: Optional[WebSDRStatus] = None
    fatal_error: Optional[str] = None
    # v11 reverse sync (WebSDR -> rig): reverse_sync_error is FINAL/
    # actionable (the retry ladder gave up after REVERSE_PUSH_MAX_ATTEMPTS
    # and reverted the WebSDR page to match the rig -- rig status is
    # master), cleared on the next successful reverse push or fresh WebSDR
    # attach. reverse_sync_pending is transient/informational (an attempt
    # already failed once and a retry is queued) -- deliberately blank on
    # the very first attempt, since a single unconfirmed readback is
    # routine CAT-bus latency on some rigs, not worth surfacing.
    reverse_sync_error: Optional[str] = None
    reverse_sync_pending: Optional[str] = None
    # v13: True while the user has paused the WebSDR -> rig direction via
    # the Hold toggle -- distinct from both fields above (this is a
    # deliberate, ongoing choice, not an error or an in-progress retry).
    reverse_sync_held: bool = False
    # GUI rewrite: True while the user has paused the rig -> WebSDR
    # direction via the Pause sync toggle -- orthogonal to
    # reverse_sync_held (that's the WebSDR -> rig direction). See
    # SyncEngine._forward_sync_paused's docstring.
    forward_sync_paused: bool = False
    # v14: True while the WebSDR was disconnected BY US for idleness (see
    # AppSettings.websdr_idle_disconnect_min) and is armed to reconnect
    # automatically. A separate field rather than a websdr.last_error
    # string for the same reason reverse_sync_held is separate: this is a
    # deliberate, healthy state, and must not render as a failure.
    websdr_idle_stopped: bool = False
    # Mirrors SyncEngine.site (None when no WebSDR session is active).
    # gui/app.py tracks its OWN _active_websdr_site, set only from its
    # click handlers (_begin_websdr_connect/_begin_websdr_switch) -- an
    # idle auto-resume (SyncEngine._resume_websdr_after_idle) brings a
    # session back with no click at all, so without these the GUI has no
    # way to learn which site actually reconnected: the strip shows a
    # blank site name and its Connect/Disconnect button reads "Load"
    # (comparing against an empty state.site) instead of "Disconnect",
    # and clicking it does a full switch/reload instead of disconnecting.
    # Split into 3 plain strings rather than a WebSDRSite object so this
    # stays a trivial dataclass-of-primitives like every other field here.
    websdr_site_name: Optional[str] = None
    websdr_site_url: Optional[str] = None
    websdr_site_driver_type: Optional[str] = None


class SyncEngine:
    """One instance per GUI session (created once at app startup, lives
    until the window closes). settings.rigctld_host/port/flrig_host/
    flrig_port/use_mock_rig/rig_backend are read fresh each time
    start_rig_from_other_thread() is called (its caller passes the
    current values explicitly), not cached at construction, since a
    whole SyncEngine is no longer recreated per Connect click."""

    def __init__(
        self, settings: AppSettings, status_queue: "queue.Queue[GuiMessage]", webview_host: WebViewHost
    ) -> None:
        self.settings = settings
        self.status_queue = status_queue
        self._webview_host = webview_host
        self.stop_event = asyncio.Event()  # whole-app shutdown (window close), not per-subsystem
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_requested = threading.Event()

        # Rig subsystem (independent lifecycle -- see module docstring).
        self._rig: Optional[RigClient] = None
        # asyncio.AbstractServer (rigctld mock) or FlrigMockServerHandle
        # (flrig mock) -- no common base class, both duck-type close() +
        # await wait_closed(), which is all _stop_rig() needs.
        self._mock_server = None
        self._mock_state: "Optional[FakeRigState | FakeFlrigState]" = None
        self._rig_active = False
        self._rig_error: Optional[str] = None
        # Bumped on every _start_rig() call -- mirrors _websdr_generation
        # below (same staleness problem, different subsystem). While the
        # rig is idle after a failed connect, _tick() keeps publishing a
        # snapshot every poll carrying the same self._rig_error (see that
        # field's docstring), so the GUI's status_queue can be holding a
        # backlog of stale "still shows the old error" snapshots at the
        # exact moment the user clicks Connect again. The GUI resets its
        # one-shot popup guard synchronously on click, but this engine call
        # only runs later, asynchronously, on this loop's own thread --
        # without a generation tag, one of those backlog snapshots gets
        # drained right after the click and re-triggers the "connection
        # failed" popup for an attempt that hasn't even started yet,
        # confirmed live even when the new attempt goes on to succeed.
        self._rig_generation = 0
        # Remembered from _start_rig()'s params, purely for the give-up
        # error message below -- RigClient itself doesn't expose these
        # uniformly enough to read back out.
        self._rig_backend: Optional[str] = None
        self._rig_host: Optional[str] = None
        self._rig_port: Optional[int] = None
        # Set in _start_rig(), cleared on the first successful
        # ensure_connected() -- see RIG_CONNECT_TIMEOUT_S above.
        self._rig_connect_deadline: Optional[float] = None

        # WebSDR subsystem (independent lifecycle).
        self._page: Optional[Page] = None
        self._driver = None
        self.site: Optional[WebSDRSite] = None
        self._attach_task: Optional[asyncio.Task] = None
        self._websdr_active = False
        # Bumped on every _start_websdr()/_stop_websdr() -- a WebView's
        # on_dead callback captures the generation it was created under,
        # so a stale dead-notification from an already-replaced/torn-down
        # page can't trigger a spurious recreation of whatever's active now.
        self._websdr_generation = 0
        # v14 attach-retry ladder state. Lives on the ENGINE, not in
        # _attach_supervisor()'s local scope, so it survives the
        # supervisor task being torn down and recreated -- otherwise the
        # _handle_page_dead() -> _start_websdr() path launders the whole
        # failure count on every crash-recreate cycle and the backoff
        # never grows. Only a genuinely user-initiated Connect/Switch
        # (see _start_websdr(user_initiated=True)) or an attachment that
        # stayed up ATTACH_STABLE_S clears them.
        self._attach_failures: int = 0
        self._attach_attached_at: Optional[float] = None
        self._attach_first_failure_at: Optional[float] = None
        # Non-fatal "still retrying" text, folded into the published
        # WebSDRStatus.last_error by _publish() -- see
        # ATTACH_STILL_TRYING_FAILURES.
        self._attach_status_message: Optional[str] = None

        # v14 idle disconnect -- releases a scarce audio slot on a
        # volunteer-run receiver when nobody is actually using it. Keyed
        # on RIG activity, not page activity: the rig is the meaningful
        # "is a human here" signal, and it's polled every tick regardless
        # of whether a WebSDR session exists.
        # "Just happened" rather than 0.0, same reasoning as
        # _forward_push_completed_at's reset: a fresh engine must not
        # start out already counting as idle.
        self._last_rig_activity_at: float = time.monotonic()
        # Deliberately separate from the forward-push dedupe latches: this
        # needs a raw, ungated view of "did anything about the rig change
        # at all", not one filtered by debounce/threshold/transmitting.
        self._idle_last_freq: Optional[int] = None
        self._idle_last_mode: Optional[str] = None
        self._idle_last_ptt: Optional[bool] = None
        # True only while a session we stopped for idleness is armed to
        # auto-resume; cleared by every non-idle stop and every
        # user-initiated start, so a manual Disconnect can never be
        # silently undone moments later.
        self._websdr_idle_stopped: bool = False
        self._idle_stopped_site: Optional[WebSDRSite] = None
        # Whether _idle_stopped_site was findable in the user's site
        # lists at the moment we idle-stopped. Needed to tell a site the
        # user DELETED during the idle window (was listed, now gone --
        # don't reconnect to it) apart from a one-off Custom URL that was
        # never in any list to begin with (never listed -- reconnecting
        # to the snapshot is the only correct behavior). Without this
        # distinction, "not found at resume time" would strand every
        # unsaved custom-URL session.
        self._idle_stopped_site_was_listed: bool = False
        # Floor on how often a FAILED idle resume may be retried. The
        # retry is driven by rig activity, and spinning a VFO produces
        # activity several times a second -- against a site that fails to
        # start at all (unknown driver_type, WebView creation failing)
        # that would mean several page-creation attempts per second plus
        # a log line each. Bounded here rather than by giving up, since
        # giving up is what Finding 3 exists to prevent.
        self._idle_resume_retry_at: float = 0.0
        # Whether the previous tick found the rig reachable. A tick that
        # observes not-connected -> connected treats that transition
        # ITSELF as rig activity, regardless of whether freq/mode/ptt
        # actually differ: _idle_last_* keep their pre-drop values across
        # an outage, so a rig that comes back reporting exactly what it
        # showed before (unplugged and replugged without touching the
        # dial, PC resumed from sleep) would otherwise register no
        # change at all and an idle-stopped session would never resume.
        self._rig_was_connected_last_tick: bool = False

        # cw_offset_hz as last applied to self._driver -- None until the
        # first driver exists. Compared against self.settings.cw_offset_hz
        # each tick so a LIVE change (the user editing the Behaviour-tab
        # spinner mid-session) can be told apart from "just attached, this
        # is what the driver was already constructed with" -- see _tick()'s
        # own use of this for why that distinction matters (a live change
        # must reseed the reverse-sync baseline, not be read as a page
        # edit).
        self._applied_cw_offset_hz: Optional[int] = None

        # Sync dedupe latches -- reset whenever either subsystem
        # (re)starts, since "already sent to X" is meaningless once X has
        # changed out from under them.
        self._last_sent_freq: Optional[int] = None
        self._pending_freq: Optional[int] = None
        self._pending_freq_since: float = 0.0
        # Diagnostic only (v2.2.2): tracks the last rig freq_hz value this
        # instance already logged, so the forward-push trace below logs on
        # a real CHANGE instead of every poll tick. Added to trace a live
        # report that a rig's own single-Hz step button never reaches the
        # WebSDR even after FREQ_CHANGE_THRESHOLD_HZ was lowered to 0 --
        # this narrows down whether the raw rig reading itself carries
        # Hz-level resolution at all (some rigctld/hamlib backends round
        # their own reported frequency) versus the engine seeing the
        # right value but not acting on it.
        self._last_logged_rig_freq: Optional[int] = None
        self._last_sent_mode_key: Optional[tuple[str, Optional[int]]] = None
        self._last_ptt: Optional[bool] = None
        # See FULL_RESYNC_INTERVAL_S.
        self._last_full_resync_at: float = time.monotonic()
        # See WEBSDR_MIN_WRITE_GAP_S -- 0.0 ("long ago") so the very first
        # forward push is never held off by this gate. Exact counterpart
        # of _last_rig_write_at on the reverse side.
        self._last_websdr_write_at: float = 0.0
        # Per-axis forward-push failure ladders -- see _ForwardPushBackoff.
        self._forward_freq_backoff = _ForwardPushBackoff()
        self._forward_mode_backoff = _ForwardPushBackoff()
        # "Did I just cause this WebSDR value" bookkeeping -- set only on
        # a successful FORWARD (rig -> WebSDR) push, in rig-native units
        # (hamlib mode name / Hz), so reverse sync can recognize its own
        # forward-pushed values echoing back through a stale page read
        # (Blocker-1) as distinct from _last_sent_freq/_last_sent_mode_key,
        # which answer "should I re-push to WebSDR" instead.
        self._last_pushed_to_websdr_freq: Optional[int] = None
        self._last_pushed_to_websdr_mode: Optional[str] = None
        # Set after EITHER a successful forward push OR a successful
        # reverse push -- see REVERSE_HOLDOFF_S.
        self._forward_push_completed_at: float = 0.0
        # Set alongside _forward_push_completed_at whenever a FORWARD
        # push actually changes something -- tells the next
        # reverse-eligible tick (once the hold-off has elapsed) to
        # re-seed its observed/pushed bookkeeping from whatever the page
        # shows THEN, rather than compare against pre-push values. This
        # is what actually closes the "many-to-one forward mapping"
        # gap: _last_pushed_to_websdr_mode stores the rig-native mode
        # (e.g. "PKTUSB"), but the page's own normalized mode after that
        # push is the canonical WebSDR-side form (e.g. "USB") --
        # comparing those directly can never match, so a straight
        # equality check alone would misread the push's own echo as a
        # fresh user edit. Re-seeding on the first post-push observation
        # sidesteps needing each driver's forward-collapse table at the
        # engine level.
        #
        # "Actually changes something" is load-bearing, not just a
        # description -- the two set-sites below must gate on the push
        # representing a REAL change (mode_key/pending_freq differing from
        # what was last sent), not merely on the push having "succeeded".
        # due_for_periodic_resync (FULL_RESYNC_INTERVAL_S) forces a push
        # even when nothing changed, purely as a safety net -- arming this
        # flag for THAT case too (a real bug, fixed after being found live)
        # opened a REVERSE_HOLDOFF_S window every ~30s where a genuine page
        # click landing in it got silently adopted as the new baseline
        # instead of triggering a real reverse push to the rig.
        self._reverse_reseed_due: bool = False
        # Forward-push counterpart of _sync_latch_generation, checked by
        # _tick()'s forward block after each await into the driver.
        # Deliberately a SEPARATE counter rather than reusing the reverse
        # one: _apply_reverse_sync_held() bumps _sync_latch_generation
        # (correctly, for reverse sync's own in-flight state), and the
        # Hold toggle's own contract says forward sync is left completely
        # untouched -- sharing the counter would let a Hold click landing
        # mid-await inside set_mode()/tune_hz() discard a forward push's
        # bookkeeping, which is accidental, not designed. Bumped ONLY by
        # _reset_sync_latches(), the one event that genuinely invalidates
        # forward bookkeeping (the page it referred to is gone).
        self._forward_latch_generation: int = 0

        # v11 reverse sync (WebSDR -> rig) latches.
        self._last_observed_freq: Optional[int] = None
        self._pending_reverse_freq: Optional[int] = None
        self._pending_reverse_freq_since: float = 0.0
        self._last_observed_mode_key: Optional[str] = None
        # True once the first post-(re)attach/toggle-on observation has
        # been captured as a baseline (not pushed) -- see
        # _reverse_sync_tick()'s docstring for why the first observation
        # must never be treated as a user action.
        self._reverse_baseline_captured: bool = False
        # Rate-limits repeated-identical-unmapped-mode logging, same
        # pattern as each driver's own _last_unmapped_mode -- distinct
        # from the retry ladders below, which handle genuinely mapped
        # values the rig didn't confirm, not values with no mapping at all.
        self._last_unmapped_reverse_mode: Optional[str] = None
        # In-flight (or backing-off-between-attempts) retry ladders --
        # None means no push is currently being attempted for that axis.
        # See REVERSE_PUSH_* constants and _reverse_sync_tick().
        self._mode_push: Optional[_ReversePush] = None
        self._freq_push: Optional[_ReversePush] = None
        # See REVERSE_MIN_RIG_WRITE_GAP_S -- 0.0 ("long ago") so the very
        # first eligible reverse push is never held off by this gate.
        self._last_rig_write_at: float = 0.0
        # Debounces a discrete mode click the same way _pending_reverse_freq/
        # _pending_reverse_freq_since debounce a continuous drag.
        self._pending_reverse_mode: Optional[str] = None
        self._pending_reverse_mode_since: float = 0.0
        self._reverse_sync_error: Optional[str] = None
        # Bumped by _reset_sync_latches() -- _reverse_sync_tick() checks
        # this after each await (set_mode()/set_freq() to the rig) before
        # writing any latch, since _attach_supervisor can call
        # _reset_sync_latches() from a different task on the same loop
        # while that await is in flight; without this guard, the
        # post-await write would resurrect pre-reattach bookkeeping over
        # a reset that already superseded it.
        self._sync_latch_generation: int = 0
        # Session-only pause on the WebSDR -> rig direction (v13's
        # "Hold" toggle) -- deliberately NOT reset by
        # _reset_sync_latches()/a WebSDR reattach, and NOT persisted to
        # AppSettings: always starts False on a fresh launch, so the app
        # never silently starts up already holding reverse sync from a
        # forgotten previous session. See set_reverse_sync_held().
        self._reverse_sync_held: bool = False
        # v15/GUI-rewrite Pause sync toggle (rig -> WebSDR direction).
        # Orthogonal to _reverse_sync_held -- this blocks the forward
        # PUSH only (see the "not self._forward_sync_paused and" guards
        # in _tick()'s mode/freq push conditions), never rig/WebSDR
        # polling itself, and pending-freq/mode bookkeeping keeps
        # accumulating while paused so unpausing fires an immediate
        # corrective push through the existing debounce/threshold check
        # rather than needing its own latch-clearing. Session-only, same
        # reasoning as _reverse_sync_held: never persisted, always False
        # on a fresh launch. See set_forward_sync_paused_from_other_thread().
        self._forward_sync_paused: bool = False

    # ------------------------------------------------------------------
    # Thread-safe entry points -- call these from the GUI thread.
    def stop_from_other_thread(self) -> None:
        """Ends the whole session (app window closing), tearing down both
        subsystems. Individual subsystems are stopped via
        stop_rig_from_other_thread()/stop_websdr_from_other_thread()
        instead -- this is NOT what a per-panel Disconnect button calls."""
        self._stop_requested.set()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self.stop_event.set)
            except RuntimeError:
                pass  # loop already closed (engine thread already exited)

    def start_rig_from_other_thread(self, backend: str, host: str, port: int, use_mock: bool) -> None:
        if self._loop is None:
            return
        self._run_coro_threadsafe(self._start_rig(backend, host, port, use_mock))

    def stop_rig_from_other_thread(self) -> None:
        if self._loop is None:
            return
        self._run_coro_threadsafe(self._stop_rig())

    def start_websdr_from_other_thread(self, site: WebSDRSite) -> None:
        """The initial Connect (or a recovery from a dead WebView) -- if a
        session is already active, _start_websdr() tears the whole page
        down and rebuilds it. switch_websdr_from_other_thread() below is
        the entry point for changing sites while already connected; it
        does NOT go through this."""
        if self._loop is None:
            return
        self._run_coro_threadsafe(self._start_websdr(site))

    def switch_websdr_from_other_thread(self, site: WebSDRSite) -> None:
        """Loads a different site on the SAME already-open WebView instead
        of tearing it down and rebuilding it -- see _switch_websdr()'s own
        docstring for why. The GUI only calls this while already
        connected; _switch_websdr() falls back to a full _start_websdr()
        on its own if that's somehow not the case."""
        if self._loop is None:
            return
        self._run_coro_threadsafe(self._switch_websdr(site))

    def stop_websdr_from_other_thread(self) -> None:
        """A manual Disconnect. _stop_websdr()'s default
        idle_disconnect=False is what disarms the v14 idle auto-resume --
        without that, a session the user just chose to end could quietly
        reconnect itself the moment they touched the VFO."""
        if self._loop is None:
            return
        self._run_coro_threadsafe(self._stop_websdr())

    def _run_coro_threadsafe(self, coro) -> None:
        # self._loop is re-read (not passed in) since the caller checked it
        # moments ago under no lock -- still guard the actual scheduling
        # call against it having closed in between.
        loop = self._loop
        if loop is None:
            coro.close()  # avoid a "coroutine was never awaited" warning
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()

    # ------------------------------------------------------------------
    def _publish(self, **overrides) -> None:
        """Builds a snapshot from current subsystem state plus whatever the
        caller overrides (e.g. rig_connected/rig_freq_hz/websdr for the
        per-tick values _tick() computed -- rig_active/rig_error/
        websdr_active always come from self, never need overriding)."""
        overrides.setdefault("rig_connected", False)
        # Fold the attach supervisor's non-fatal "still retrying" text into
        # the WebSDR status the GUI already renders, rather than adding a
        # parallel channel for it. Only while NOT connected, and only as
        # the last_error slot's content -- the driver's own attach
        # exception is already in the log for diagnosis; what the user
        # needs on screen at this point is "still trying", not a repeated
        # stack-adjacent string that reads like a crash.
        websdr_status = overrides.get("websdr")
        if websdr_status is not None and self._attach_status_message and not websdr_status.connected:
            overrides["websdr"] = replace(websdr_status, last_error=self._attach_status_message)
        snapshot = StatusSnapshot(
            rig_active=self._rig_active,
            rig_error=self._rig_error,
            rig_generation=self._rig_generation,
            websdr_active=self._websdr_active,
            reverse_sync_error=self._reverse_sync_error,
            reverse_sync_pending=self._reverse_sync_pending_text(),
            reverse_sync_held=self._reverse_sync_held,
            forward_sync_paused=self._forward_sync_paused,
            websdr_idle_stopped=self._websdr_idle_stopped,
            websdr_site_name=self.site.name if self.site is not None else None,
            websdr_site_url=self.site.url if self.site is not None else None,
            websdr_site_driver_type=self.site.driver_type if self.site is not None else None,
            rig_connect_remaining_s=(
                max(0.0, self._rig_connect_deadline - time.monotonic())
                if self._rig_connect_deadline is not None else None
            ),
            **overrides,
        )
        try:
            self.status_queue.put_nowait(snapshot)
        except queue.Full:
            pass

    def _reverse_sync_pending_text(self) -> Optional[str]:
        """Computed fresh on every publish (not a stored field) so it can
        never go stale across the ladder's several return points. Blank
        on attempt 1 deliberately -- a single unconfirmed readback is
        routine CAT-bus latency on some rigs, not worth alarming over."""
        if self._mode_push is not None and self._mode_push.attempts >= 1:
            return (
                f"Setting mode {self._mode_push.target!r} on the rig... "
                f"(attempt {self._mode_push.attempts + 1}/{REVERSE_PUSH_MAX_ATTEMPTS})"
            )
        if self._freq_push is not None and self._freq_push.attempts >= 1:
            return (
                f"Setting frequency {self._freq_push.target} Hz on the rig... "
                f"(attempt {self._freq_push.attempts + 1}/{REVERSE_PUSH_MAX_ATTEMPTS})"
            )
        return None

    def publish_fatal_error(self, message: str) -> None:
        """Thread-safe: status_queue is a plain queue.Queue, safe to call
        from whichever thread is running (or failed to start) the engine.
        Reserved for the whole background thread dying unexpectedly -- a
        WebSDR/rig subsystem failing to start is reported through their own
        _start_*()'s error fields instead, not this."""
        try:
            self.status_queue.put_nowait(StatusSnapshot(fatal_error=message))
        except queue.Full:
            pass

    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Runs for the entire GUI session. Starts with both subsystems
        idle; start_rig_from_other_thread()/start_websdr_from_other_thread()
        bring them up independently, at any time, in any order."""
        self._loop = asyncio.get_running_loop()
        if self._stop_requested.is_set():
            logger.info("Stop was requested before the engine started; exiting immediately")
            return

        try:
            await self._poll_loop()
        finally:
            await self._stop_websdr()
            await self._stop_rig()

    # ------------------------------------------------------------------ rig
    async def _start_rig(self, backend: str, host: str, port: int, use_mock: bool) -> None:
        """backend is "rigctld" or "flrig" -- a simple if/else branch below,
        not a registry, since there are only ever exactly 2 (unlike the
        WebSDR side's DRIVERS dict, which has room to grow via
        fingerprinting). Mock mode is always server-side only: even in
        mock mode, self._rig is the real client class for the selected
        backend, pointed at the just-started embedded mock server -- never
        a separate fake client."""
        # Bump FIRST, before either outcome below (mock-bind failure or a
        # real attempt) -- see self._rig_generation's docstring. Every
        # snapshot this attempt publishes, including a same-tick mock-bind
        # failure, must carry this attempt's generation so the GUI can
        # tell it apart from backlog belonging to whatever attempt (if
        # any) came before this call.
        self._rig_generation += 1
        if self._rig_active:
            await self._stop_rig()

        if use_mock:
            try:
                if backend == "flrig":
                    self._mock_server, self._mock_state = await start_mock_flrig(host, port)
                else:
                    self._mock_server, self._mock_state = await start_mock_rigctld(host, port)
            except OSError as e:
                self._rig_error = (
                    f"Could not start the embedded mock rig on {host}:{port} ({e}). "
                    f"Is a real {backend} already running on that port?"
                )
                logger.error(self._rig_error)
                self._publish()
                return

        if backend == "flrig":
            self._rig = FlrigClient(host, port)
        else:
            self._rig = RigctldClient(host, port)
        self._rig_active = True
        self._rig_error = None
        self._last_ptt = None
        self._rig_backend, self._rig_host, self._rig_port = backend, host, port
        self._rig_connect_deadline = time.monotonic() + RIG_CONNECT_TIMEOUT_S
        logger.info("Rig session started (%s, %s:%d, mock=%s)", backend, host, port, use_mock)
        self._publish()

    async def _stop_rig(self, error: Optional[str] = None) -> None:
        # A WebSDR session with no rig behind it has nothing driving its
        # sync -- stopping the rig (whether by explicit user Disconnect or
        # the connect-timeout give-up above) always stops WebSDR too. This
        # is a deliberate, one-directional exception to the rig/WebSDR
        # "independent lifecycles" principle (see module docstring/v3.6
        # history): that principle is about not killing rig when
        # *switching WebSDR sites*, not about this -- WebSDR already can't
        # even start without rig active (gui/app.py's Connect button is
        # gated on it), so it stopping when rig stops is consistent with,
        # not a regression of, that existing design.
        if self._websdr_active:
            await self._stop_websdr()
        # Unconditionally, OUTSIDE the cascade above: while a session is
        # idle-stopped, _websdr_active is already False, so the cascade
        # is skipped and the auto-resume bookkeeping would survive an
        # explicit rig Disconnect -- leaving the app free to silently
        # reopen a session on someone else's receiver hours later, the
        # first time the rig is touched again, with no Connect click.
        # _stop_websdr()'s docstring promises every non-idle stop path
        # disarms auto-resume; this is that promise for the rig-stop path.
        self._websdr_idle_stopped = False
        self._idle_stopped_site = None
        # Also unconditional, same reasoning: a WebSDR _start_websdr() can
        # be mid-flight (suspended in its own create_page() await) with
        # _websdr_active still False -- the cascade above only fires once
        # that flag is already True, which it isn't yet during that
        # window. _start_websdr()'s own post-await generation re-check is
        # what actually catches this, but only if this bump happens here;
        # without it, a rig Disconnect landing during that window is
        # silently undone once the in-flight start resumes and commits a
        # full WebSDR session against a rig that's already gone.
        self._websdr_generation += 1
        if self._rig is not None:
            await self._rig.close()
            self._rig = None
        if self._mock_server is not None:
            # wait_closed() so a fast stop+restart cycle doesn't race a
            # still-unbinding port.
            self._mock_server.close()
            await self._mock_server.wait_closed()
            self._mock_server = None
            self._mock_state = None
        was_active = self._rig_active
        self._rig_active = False
        # So the next session's first successful connect registers as the
        # not-connected -> connected activity edge (see _tick()).
        self._rig_was_connected_last_tick = False
        self._rig_error = error
        self._rig_connect_deadline = None
        if was_active and error is None:
            logger.info("Rig session stopped")
        self._publish()

    # ------------------------------------------------------------------ websdr
    async def _start_websdr(self, site: WebSDRSite, user_initiated: bool = True) -> bool:
        """user_initiated=False marks an INTERNAL restart (recovering from
        a dead WebView), which must NOT reset the attach-failure ladder --
        that's the exact laundering path that let a site failing every
        few seconds be retried forever at the base delay. Defaults to True
        so the GUI-facing entry point (start_websdr_from_other_thread)
        needs no change: a real Connect/Switch click is a fresh human
        decision and deserves a clean ladder.

        Returns True if this call's own outcome (success OR a genuine
        failure) is authoritative and the caller should act on it; False
        only for the superseded case below, where a concurrent event
        already decided this attempt's fate and its outcome must be
        ignored. _resume_websdr_after_idle() is the one caller that needs
        this distinction -- without it, "was I superseded" and "did I
        genuinely fail" are both just "_websdr_active is still False" from
        the caller's point of view, and re-arming auto-resume for the
        superseded case would silently undo whatever superseded it (a
        Disconnect, or a competing Connect/Switch to a different site).
        Every other caller is fire-and-forget and ignores this."""
        driver_cls = DRIVERS.get(site.driver_type)
        if driver_cls is None:
            logger.error("No WebSDR driver registered for type %r", site.driver_type)
            self._publish(websdr=WebSDRStatus(
                connected=False, last_error=f"No WebSDR driver registered for type {site.driver_type!r}"
            ))
            return True

        if self._websdr_active:
            await self._stop_websdr()

        if user_initiated:
            self._attach_failures = 0
            self._attach_attached_at = None
            self._attach_first_failure_at = None
            self._attach_status_message = None
            # A fresh manual Connect supersedes any idle-stop bookkeeping
            # left over from a previous session.
            self._websdr_idle_stopped = False
            self._idle_stopped_site = None
            # A human clicking Connect/Switch IS rig-adjacent activity.
            # Without this stamp the idle clock keeps running from
            # whenever the rig was last touched, so connecting after an
            # hour of no rig movement leaves _idle_disconnect_due() True
            # the instant the session comes up and _tick() tears it
            # straight back down one poll interval later -- the user
            # cannot connect by hand at all. Same reasoning applies to
            # the very first Connect on an app left open a long time
            # with the rig parked on one frequency.
            self._last_rig_activity_at = time.monotonic()

        self._websdr_generation += 1
        my_generation = self._websdr_generation

        try:
            page = await self._webview_host.create_page(
                self._loop, on_dead=lambda reason: self._on_page_dead(my_generation, reason)
            )
        except Exception as e:
            logger.exception("Failed to create WebView for WebSDR session")
            self._publish(websdr=WebSDRStatus(connected=False, last_error=_describe_webview_create_error(e)))
            return True

        if self._websdr_generation != my_generation:
            # Something else -- a user Disconnect, another Start/Switch --
            # bumped the generation again while create_page() was
            # suspended above (this is exactly how a page-death recovery,
            # _on_page_dead -> _handle_page_dead -> _start_websdr(user_
            # initiated=False), can race a Disconnect that runs to
            # completion in between: _handle_page_dead's own generation
            # check only guards its SCHEDULING, not the scheduled
            # coroutine's own later await). Without this, the Disconnect
            # would be silently undone: engine state ends up
            # websdr_active=True, pointed at a page the user already tore
            # down. Drop this attempt and destroy the now-orphaned page
            # instead of committing any state.
            logger.info("Dropping stale WebSDR start for %s (superseded while creating the WebView)", site.name)
            try:
                await self._webview_host.destroy_page(page, self._loop)
            except Exception:
                logger.exception("Error destroying superseded WebView")
            # Every OTHER exit from this method publishes a snapshot
            # carrying a non-None `websdr` -- gui/app.py's
            # _apply_status_snapshot() uses exactly that ("websdr_active
            # is False, but websdr is not None") as its only signal that a
            # pending Connect is genuinely OVER, not just "no news yet".
            # A silent return here left _websdr_connect_pending latched
            # forever: the strip's Connect button stuck disabled, reading
            # "Connecting..." indefinitely, with no click able to clear it
            # (reachable for real once _stop_rig() started unconditionally
            # bumping the generation for this exact race).
            self._publish(websdr=WebSDRStatus(connected=False))
            return False

        self._page = page
        self.site = site
        self._driver = driver_cls(
            site.url, cw_offset_hz=self.settings.cw_offset_hz,
            auto_click_audio_unlock=self.settings.auto_click_audio_unlock,
        )
        self._applied_cw_offset_hz = self.settings.cw_offset_hz
        self._websdr_active = True
        self._reset_sync_latches()
        # Runs for the WebSDR subsystem's whole lifetime, independently of
        # the poll loop: (re)attaches whenever the driver isn't attached,
        # with backoff on failure.
        self._attach_task = asyncio.ensure_future(self._attach_supervisor(self._page))
        logger.info("WebSDR session started: %s (%s)", site.name, site.url)
        self._publish()
        return True

    def _on_page_dead(self, generation: int, reason: str) -> None:
        """Called by WxPageAdapter (via wx.CallAfter) when a script timeout
        or other unrecoverable failure marks it permanently dead -- without
        this, a dead adapter fails every subsequent call cleanly but
        nothing ever replaces it (a real gap the Block B implementation
        review found: the WebView shim itself cannot recover on its own,
        since a stuck RunScriptAsync can never be cancelled). Runs on the
        GUI thread; marshal back onto the engine loop before touching any
        engine state."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._handle_page_dead, generation, reason)
        except RuntimeError:
            pass  # loop already closed (engine thread already exited) -- same guard as stop_from_other_thread()

    def _handle_page_dead(self, generation: int, reason: str) -> None:
        if generation != self._websdr_generation or not self._websdr_active or self.site is None:
            # Stale notification from a page that's already been replaced
            # or torn down since -- ignore it, don't recreate something
            # that isn't the current session anymore.
            return
        logger.warning("WebView for %s died (%s) -- recreating", self.site.name, reason)
        # user_initiated=False: this is internal crash recovery, not a
        # human asking for a fresh connection -- the attach-failure ladder
        # must carry across it (see _start_websdr's docstring).
        asyncio.ensure_future(self._start_websdr(self.site, user_initiated=False))

    async def _switch_websdr(self, site: WebSDRSite) -> None:
        """Loads a different site on the SAME already-open WebView/host
        frame, instead of destroying and recreating it the way
        _start_websdr()/_stop_websdr() do. Falls back to a full
        _start_websdr() if there's nothing open to switch on (the GUI
        shouldn't call this in that state, but this is the safe thing to
        do if it ever does).

        Why this exists: Switch used to just call start_websdr_from_other_
        thread() again, which (via _start_websdr()'s own "if active,
        _stop_websdr() first" guard) always tore the whole page and its
        host frame down and rebuilt them. On Windows that meant the WebSDR
        window disappeared and had to be reshown/re-raised every single
        switch -- which turned out to be the root of a string of Win32
        z-order/visibility races (the window coming up behind an
        already-maximized app, or staying invisible with its audio still
        playing) chased across several fixes in one live-testing session
        before landing on "don't tear the window down at all" as the
        actual fix, not a faster teardown.

        Reusing self._page here works because WxPageAdapter's own
        goto()/attach() machinery already treats "navigate the same
        adapter to a new URL" as a first-class, already-tested case (every
        driver's own reattach-retry loop does exactly this against the
        same page) -- nothing about the page adapter is single-navigation-
        only. The new driver's attach() does the actual page.goto() once
        its handshake against whatever's currently loaded (the OLD site)
        fails, which it always will."""
        driver_cls = DRIVERS.get(site.driver_type)
        if driver_cls is None:
            logger.error("No WebSDR driver registered for type %r", site.driver_type)
            self._publish(websdr=WebSDRStatus(
                connected=False, last_error=f"No WebSDR driver registered for type {site.driver_type!r}"
            ))
            return

        if self._page is None or not self._websdr_active:
            await self._start_websdr(site)
            return

        self._websdr_generation += 1  # invalidates any in-flight on_dead notification
        # Captured BEFORE the awaits below, and never re-read from
        # self._attach_task/self._driver afterward -- a concurrent second
        # switch (e.g. a double-click on the Sites panel's Load button,
        # which nothing disables mid-switch) can suspend here too and
        # install ITS OWN attach_task/driver on self while this call is
        # still awaiting; operating on live self.-attributes after that
        # point could cancel/close the WINNING switch's objects instead of
        # this call's own stale ones.
        my_generation = self._websdr_generation
        old_attach_task = self._attach_task
        old_driver = self._driver
        if old_attach_task is not None:
            old_attach_task.cancel()
            try:
                await old_attach_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Attach supervisor raised while switching WebSDR sites")
        if old_driver is not None:
            try:
                await old_driver.close()
            except Exception:
                logger.exception("Error closing WebSDR driver during switch")

        if self._websdr_generation != my_generation:
            # Superseded by another concurrent switch/start/stop while the
            # awaits above were in flight -- whatever superseded us
            # already owns (or is about to own) self.site/self._driver/
            # self._attach_task. Don't touch any of it: no bookkeeping
            # reset, no new driver, no second attach supervisor. Without
            # this, both calls would install their own attach_task
            # (overwriting, not cancelling, whichever the other one just
            # set), leaving two supervisors driving the same page forever.
            logger.info(
                "Dropping stale WebSDR switch to %s (superseded while tearing down the old driver)", site.name,
            )
            return

        self._attach_task = None

        # A user-picked switch is a fresh human decision, same reasoning as
        # _start_websdr(user_initiated=True)'s identical reset.
        self._attach_failures = 0
        self._attach_attached_at = None
        self._attach_first_failure_at = None
        self._attach_status_message = None
        self._websdr_idle_stopped = False
        self._idle_stopped_site = None
        self._last_rig_activity_at = time.monotonic()

        self.site = site
        self._driver = driver_cls(
            site.url, cw_offset_hz=self.settings.cw_offset_hz,
            auto_click_audio_unlock=self.settings.auto_click_audio_unlock,
        )
        self._applied_cw_offset_hz = self.settings.cw_offset_hz
        self._reset_sync_latches()
        # Re-bound against the SAME page for the new generation -- see
        # WxPageAdapter.set_on_dead()'s docstring for why this can't be
        # skipped just because create_page() (which normally wires this
        # up) isn't being called here.
        self._page.set_on_dead(lambda reason: self._on_page_dead(my_generation, reason))
        self._attach_task = asyncio.ensure_future(self._attach_supervisor(self._page))
        logger.info("WebSDR session switched to: %s (%s)", site.name, site.url)
        self._publish()

    async def _stop_websdr(self, idle_disconnect: bool = False) -> None:
        """idle_disconnect=True is the ONLY path that arms auto-resume.
        Every other stop -- a user Disconnect, the rig-stop cascade, a
        site switch, app shutdown -- disarms it, so a session the user
        deliberately ended can never silently come back."""
        self._websdr_generation += 1  # invalidates any in-flight on_dead notification
        if self._attach_task is not None:
            self._attach_task.cancel()
            try:
                await self._attach_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Attach supervisor raised while stopping WebSDR session")
            self._attach_task = None
        if self._driver is not None:
            try:
                await self._driver.close()
            except Exception:
                logger.exception("Error closing WebSDR driver")
            self._driver = None
        if self._page is not None:
            try:
                await self._webview_host.destroy_page(self._page, self._loop)
            except Exception as e:
                logger.warning("Non-fatal error destroying WebView: %s", e)
            self._page = None
        was_active = self._websdr_active
        self.site = None
        self._websdr_active = False
        self._attach_status_message = None
        # Per-ATTACHMENT state, so it must not survive a teardown --
        # unlike _attach_failures, which deliberately does (that is what
        # stops a crash-recreate cycle from laundering the ladder). Left
        # set, the next session's supervisor sees it non-None on its very
        # first loop iteration, when the new driver naturally isn't
        # attached yet, takes the flap branch, charges a bogus failure
        # and sleeps a backoff delay BEFORE ever attempting to attach the
        # new page -- delaying recovery from an unrelated WebView crash
        # and mislabelling it in the log as a dropped connection.
        self._attach_attached_at = None
        self._websdr_idle_stopped = idle_disconnect
        if not idle_disconnect:
            self._idle_stopped_site = None
        if was_active:
            logger.info("WebSDR session stopped")
        self._publish()

    # ------------------------------------------------------------------ idle disconnect (v14)
    def _note_rig_activity(self, state: RigState) -> bool:
        """Returns True if anything about the rig changed since the last
        tick. A plain != on all three values, deliberately NOT the
        debounce/threshold logic the forward push uses: that logic exists
        to bound how often we WRITE, whereas this is a coarse
        minutes-scale "is a human at the radio" signal, and filtering out
        a 5 Hz nudge here would mean someone slowly working a weak signal
        looks idle.

        Every RigState field is Optional, and a None is a DROPPED READ --
        not a value the operator moved something to. A plain != counted
        each drop as activity and each recovery as a second one, so a
        marginal USB-serial/CAT link reset the idle timer continuously
        and idle-disconnect could never fire on exactly the unattended
        setup it matters most for. A None reading is therefore skipped
        entirely and, crucially, does NOT overwrite the remembered value
        -- which also makes the recovery read compare against the real
        pre-drop value, so coming back to the same frequency is
        correctly seen as no change. A None -> real transition is still
        activity (that IS new information, and it is how the trackers
        seed on the first tick). Compared per field so a drop on one
        axis can't mask a genuine change on another."""
        changed = False
        if state.freq_hz is not None and state.freq_hz != self._idle_last_freq:
            self._idle_last_freq = state.freq_hz
            changed = True
        if state.mode is not None and state.mode != self._idle_last_mode:
            self._idle_last_mode = state.mode
            changed = True
        if state.ptt is not None and state.ptt != self._idle_last_ptt:
            self._idle_last_ptt = state.ptt
            changed = True
        if changed:
            self._last_rig_activity_at = time.monotonic()
        return changed

    def _idle_disconnect_due(self) -> bool:
        minutes = self.settings.websdr_idle_disconnect_min
        if not minutes or minutes <= 0:
            return False  # None or <= 0 means the feature is off
        return time.monotonic() - self._last_rig_activity_at >= minutes * 60

    def _resolve_site_by_url(self, url: str) -> Optional[WebSDRSite]:
        """Looks a URL up in the site definitions available RIGHT NOW --
        the user's own saved/imported/curated lists (live: the engine and
        the GUI share one AppSettings instance, and the GUI mutates these
        lists in place) plus the app's built-in defaults.

        Returns the CURRENT definition, so a driver_type the user
        corrected during an idle window is picked up on resume instead of
        the hours-old snapshot. Returns None if the URL is in no list at
        all, which the caller must not read as "deleted" on its own --
        an unsaved Custom URL is also in no list. See
        _idle_stopped_site_was_listed."""
        for entries in (
            self.settings.user_sites,
            self.settings.imported_sites,
            self.settings.curated_sites,
        ):
            for entry in entries:
                if entry.get("url") == url:
                    return WebSDRSite(
                        name=entry.get("name") or url,
                        url=url,
                        driver_type=entry["driver_type"],
                    )
        return next((s for s in KNOWN_SITES if s.url == url), None)

    async def _stop_websdr_for_idle(self) -> None:
        minutes = self.settings.websdr_idle_disconnect_min
        # Captured BEFORE the stop -- _stop_websdr() clears self.site.
        self._idle_stopped_site = self.site
        self._idle_stopped_site_was_listed = (
            self.site is not None and self._resolve_site_by_url(self.site.url) is not None
        )
        self._idle_resume_retry_at = 0.0
        logger.info(
            "Disconnecting idle WebSDR session (no rig activity for %d min) -- "
            "will auto-reconnect once the rig is used again",
            minutes,
        )
        await self._stop_websdr(idle_disconnect=True)

    async def _idle_disconnect_if_due(self) -> bool:
        """The DISCONNECT half of the idle check, factored out so it can
        run on the tick paths that never reach the resume half.

        Deliberately called from each of _tick()'s early-return paths
        rather than once at the top of _tick(): a rig that is unreachable
        is arguably the STRONGEST "nobody is here" signal there is, and
        every one of those paths used to return above the idle check, so
        a rig that dropped after connecting once (USB unplugged, rigctld
        killed, PC slept) held the receiver's audio slot forever. But
        hoisting the check to the top of _tick() unconditionally would
        also make it run BEFORE this tick's fresh rig state is read, so
        the one tick where an operator finally touches the dial after
        the threshold expires would tear the session down and rebuild it
        moments later within that same tick -- a pointless page load
        against a volunteer's server, which is the exact cost this round
        exists to reduce. Splitting it keeps both properties.

        Returns True if a session was actually stopped."""
        if self._websdr_active and not self._websdr_idle_stopped and self._idle_disconnect_due():
            await self._stop_websdr_for_idle()
            return True
        return False

    async def _resume_websdr_after_idle(self) -> None:
        if time.monotonic() < self._idle_resume_retry_at:
            return  # a previous resume attempt failed very recently -- see IDLE_RESUME_RETRY_GAP_S
        site = self._idle_stopped_site
        was_listed = self._idle_stopped_site_was_listed
        self._websdr_idle_stopped = False
        self._idle_stopped_site = None
        if site is None:
            return

        # The snapshot may be hours old, and the user may have edited or
        # deleted that site's definition in the meantime. Prefer the
        # CURRENT definition; only treat "not found" as deletion if the
        # site was actually in a list when we stopped it (otherwise it is
        # simply an unsaved Custom URL, which is in no list by nature).
        current = self._resolve_site_by_url(site.url)
        if current is not None:
            site = current
        elif was_listed:
            logger.info(
                "Not resuming the idle-stopped WebSDR session: %s (%s) is no longer "
                "in the site list -- reconnecting to a deleted site's stale definition "
                "would be wrong, so auto-resume is being disarmed",
                site.name, site.url,
            )
            self._publish()
            return

        logger.info("Rig activity detected -- reconnecting the WebSDR session to %s", site.name)
        # user_initiated=True, deliberately. The flag's purpose is "is
        # this a fresh decision to connect, or an internal retry of
        # something that just failed" -- and an idle disconnect is neither
        # a failure nor a crash: it was a planned, healthy release, and
        # what triggers this resume is a human physically touching the
        # radio, which is as user-initiated as a Connect click. Any
        # attach-failure history from before the idle window is minutes
        # stale and no longer describes the network, so starting the
        # ladder clean is right; the ladder rebuilds within seconds if the
        # site really is still down.
        # authoritative=False means _start_websdr() was superseded by a
        # concurrent event (a Disconnect, a competing Connect/Switch click)
        # while create_page() was in flight -- see that method's own
        # docstring for why this must NOT be treated as a failure: without
        # it, re-arming here would silently undo the Disconnect (the exact
        # thing _stop_rig()'s unconditional generation bump exists to
        # prevent), or clobber a session the user just switched to by hand.
        authoritative = await self._start_websdr(site, user_initiated=True)
        if authoritative and not self._websdr_active:
            # _start_websdr() has early-return failure paths (no driver
            # registered for the site's type, create_page() raising). We
            # already cleared the idle-stopped flags above, so without
            # this re-arm a single failed resume is TERMINAL: nothing
            # retries, _websdr_active stays False, and WebSDR sync is
            # silently over until a human notices and clicks Connect.
            # Re-arming gives the resume a natural retry on the next tick
            # that sees rig activity.
            logger.warning(
                "Could not resume the idle-stopped WebSDR session to %s -- "
                "staying armed to retry on the next rig activity",
                site.name,
            )
            self._websdr_idle_stopped = True
            self._idle_stopped_site = site
            self._idle_stopped_site_was_listed = was_listed
            self._idle_resume_retry_at = time.monotonic() + IDLE_RESUME_RETRY_GAP_S
            self._publish()

    def _attach_retry_delay(self) -> float:
        """Current ladder delay, with jitter. Deliberately ONE helper
        rather than the delay being recomputed at each of the supervisor's
        several retry points -- the pre-v14 code had two near-identical
        copies of this expression and adding jitter to only one of them
        would have been an easy and invisible mistake."""
        delay = min(
            ATTACH_RETRY_BASE_DELAY_S * (
                2 ** min(max(self._attach_failures - 1, 0), BACKOFF_EXPONENT_CAP)
            ),
            ATTACH_RETRY_MAX_DELAY_S,
        )
        return delay * random.uniform(*ATTACH_RETRY_JITTER)

    def _register_attach_failure(self) -> float:
        """Counts one failed attach cycle and returns how long to wait."""
        self._attach_failures += 1
        self._attach_attached_at = None
        if self._attach_first_failure_at is None:
            self._attach_first_failure_at = time.monotonic()
        delay = self._attach_retry_delay()
        self._update_attach_status_message(delay)
        return delay

    def _update_attach_status_message(self, delay: float) -> None:
        """Non-fatal, deliberately worded as still-trying: this state is
        'the far end isn't answering', which for a public receiver is
        routine, not a crash. See ATTACH_STILL_TRYING_FAILURES."""
        if self._attach_failures < ATTACH_STILL_TRYING_FAILURES:
            return
        elapsed = time.monotonic() - (self._attach_first_failure_at or time.monotonic())
        self._attach_status_message = (
            f"WebSDR site not responding for {_format_duration(elapsed)} "
            f"({self._attach_failures} attempts) -- still retrying, next attempt in "
            f"{_format_duration(delay)}"
        )

    async def _attach_supervisor(self, page: Page) -> None:
        while not self.stop_event.is_set() and self._websdr_active:
            if self._driver is None:
                return  # subsystem was stopped out from under this task
            if not self._driver.attached:
                if self._attach_attached_at is not None:
                    # We DID attach, and it dropped again before it ever
                    # became stable -- a flap. Count it exactly like an
                    # attach() that raised: a receiver that accepts us and
                    # kicks us seconds later (which is what a server-side
                    # fair-use limit looks like from here) must push the
                    # ladder up, not keep resetting it to the base delay.
                    delay = self._register_attach_failure()
                    logger.warning(
                        "WebSDR attachment dropped after less than %.0fs (attempt %d) -- retrying in %.1fs",
                        ATTACH_STABLE_S, self._attach_failures, delay,
                    )
                    await self._sleep_or_stop(delay)
                    continue
                if self._attach_failures >= ATTACH_PREFLIGHT_GATE_FAILURES and self.site is not None:
                    # Deep in the ladder: spend one cheap HTTP GET before
                    # spending a whole page load (+ JS wait + WebSocket) on
                    # a site that may simply be down. A failed cheap check
                    # counts as a failure too -- it IS a failed connection
                    # attempt against the far end, and not counting it
                    # would freeze the ladder at this rung forever,
                    # defeating the extended ceiling entirely.
                    #
                    # KNOWN LIMITATION: this cheap check treats ANY HTTP
                    # response, 4xx included, as "reachable" -- so a
                    # receiver actively refusing us (403, or a fair-use
                    # block, which is precisely the signal this round
                    # exists to respect) still passes the gate and we
                    # still spend a full page load. The gate only filters
                    # a site that is down at the transport level.
                    ok, message = await check_websdr_url(self.site.url)
                    if not ok:
                        delay = self._register_attach_failure()
                        logger.warning(
                            "Skipping WebSDR attach attempt %d -- reachability check failed (%s); "
                            "retrying in %.1fs",
                            self._attach_failures, message, delay,
                        )
                        await self._sleep_or_stop(delay)
                        continue
                try:
                    await self._driver.attach(page)
                except asyncio.CancelledError:
                    raise
                except (WebSDRIncompatibleError, PlaywrightError) as e:
                    delay = self._register_attach_failure()
                    logger.warning(
                        "WebSDR attach attempt %d failed: %s -- retrying in %.1fs",
                        self._attach_failures, e, delay,
                    )
                    await self._sleep_or_stop(delay)
                    continue
                except Exception:
                    # Any other driver bug must not silently kill this task:
                    # that would leave the poll loop running forever with a
                    # permanently-unattached driver and nothing retrying.
                    # Treat it the same as a known attach failure.
                    delay = self._register_attach_failure()
                    logger.exception(
                        "WebSDR attach attempt %d raised an unexpected error -- retrying in %.1fs",
                        self._attach_failures, delay,
                    )
                    await self._sleep_or_stop(delay)
                    continue
                else:
                    if self._attach_failures:
                        logger.info(
                            "WebSDR attach succeeded after %d failed attempt(s) -- "
                            "the ladder resets once it stays up %.0fs",
                            self._attach_failures, ATTACH_STABLE_S,
                        )
                    # NOT self._attach_failures = 0 here: a success that
                    # doesn't last isn't a recovery (see ATTACH_STABLE_S).
                    # The reset happens below, once it has actually held.
                    self._attach_attached_at = time.monotonic()
                    self._attach_status_message = None
                    # The engine may have "sent" freq/mode/ptt during the
                    # outage -- those were no-ops, so the debounce/dedupe
                    # latches must be cleared or the engine will wrongly
                    # believe the WebSDR is already up to date and go silent
                    # until the rig's value physically changes again.
                    self._reset_sync_latches()
            elif (
                self._attach_attached_at is not None
                and time.monotonic() - self._attach_attached_at >= ATTACH_STABLE_S
            ):
                if self._attach_failures:
                    logger.info(
                        "WebSDR attachment stable for %.0fs -- attach retry ladder reset", ATTACH_STABLE_S
                    )
                self._attach_failures = 0
                self._attach_first_failure_at = None
                self._attach_status_message = None
                # Back to None so this can't fire repeatedly, and so a
                # later drop from a healthy session is counted as a fresh
                # first failure rather than as a flap.
                self._attach_attached_at = None
            await self._sleep_or_stop(ATTACH_CHECK_INTERVAL_S)

    async def _sleep_or_stop(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------ mock rig control (GUI-driven)
    def _push_to_mock(self, setter) -> None:
        """Thread-safe: guarded the same way as the other *_from_other_thread() methods."""
        if self._mock_state is None:
            return
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(setter)
        except RuntimeError:
            pass  # loop already closed

    def push_mock_freq(self, freq_hz: int) -> None:
        state = self._mock_state
        if state is not None:
            self._push_to_mock(lambda: setattr(state, "freq_hz", freq_hz))

    def push_mock_mode(self, mode: str, passband_hz: int) -> None:
        state = self._mock_state
        if state is None:
            return

        def _set() -> None:
            state.mode = mode
            state.passband_hz = passband_hz

        self._push_to_mock(_set)

    def push_mock_ptt(self, is_tx: bool) -> None:
        state = self._mock_state
        if state is not None:
            self._push_to_mock(lambda: setattr(state, "ptt", "1" if is_tx else "0"))

    # ------------------------------------------------------------------ Hold / rig-is-master (GUI-driven)
    def set_reverse_sync_held(self, held: bool) -> None:
        """Thread-safe. Pauses/resumes the WebSDR -> rig direction only --
        forward sync and both connections (rig, WebSDR) are completely
        untouched. If the engine loop hasn't started yet, sets the field
        directly (safe: nothing is reading it concurrently before the
        loop exists); once it's running, dispatches onto the loop so the
        accompanying ladder-state clear (see _apply_reverse_sync_held)
        can't race a concurrent _reverse_sync_tick()."""
        loop = self._loop
        if loop is None:
            self._reverse_sync_held = held
            return
        try:
            loop.call_soon_threadsafe(self._apply_reverse_sync_held, held)
        except RuntimeError:
            pass  # loop already closed

    def _apply_reverse_sync_held(self, held: bool) -> None:
        """Runs on the engine loop thread. Clears in-flight reverse-sync
        ladder/pending state on every transition (engaging OR releasing
        Hold) -- reuses the same fields _reset_sync_latches() already
        resets for exactly this reason: don't resume chasing a target
        that went stale while paused, and don't let a page click made
        while held get treated as a fresh edit the instant Hold is
        released. Scoped to just the reverse-side fields, so forward-sync
        bookkeeping is untouched.

        Also bumps _sync_latch_generation, for the same reason
        _reset_sync_latches() does: a push suspended mid-await inside
        rig.set_freq()/set_mode() (the verify-readback loop) would
        otherwise pass the generation check on return and write
        bookkeeping -- including a give-up _reverse_sync_error -- for a
        push the user just cancelled with Hold, leaving the GUI showing
        both "Reverse sync held" and a stale failure that only clears on
        release or WebSDR reattach. Bumping here makes any such in-flight
        push count as superseded, exactly like a concurrent reset. Note
        this cancels only the BOOKKEEPING: a write already on the wire to
        the rig still completes, which is unavoidable -- it can't be
        recalled mid-transmission."""
        self._reverse_sync_held = held
        self._mode_push = None
        self._freq_push = None
        self._pending_reverse_mode = None
        self._pending_reverse_mode_since = 0.0
        self._pending_reverse_freq = None
        self._pending_reverse_freq_since = 0.0
        self._reverse_baseline_captured = False
        self._reverse_sync_error = None
        self._sync_latch_generation += 1

    # ------------------------------------------------------------------ Pause sync (rig -> WebSDR, GUI-driven)
    def set_forward_sync_paused_from_other_thread(self, paused: bool) -> None:
        """Thread-safe. Pauses/resumes the rig -> WebSDR direction only --
        reverse sync, mute-on-TX, and both connections are completely
        untouched (see _forward_sync_paused's own docstring for why no
        latch-clearing is needed here, unlike set_reverse_sync_held).
        Mirrors that method's loop-dispatch pattern exactly: set directly
        if the engine loop hasn't started yet (nothing reads it
        concurrently before the loop exists), otherwise dispatch onto the
        loop so a concurrent _tick() can't read a half-updated value."""
        loop = self._loop
        if loop is None:
            self._forward_sync_paused = paused
            return
        try:
            loop.call_soon_threadsafe(setattr, self, "_forward_sync_paused", paused)
        except RuntimeError:
            pass  # loop already closed

    # ------------------------------------------------------------------
    def _reset_sync_latches(self) -> None:
        self._last_sent_freq = None
        self._pending_freq = None
        self._last_sent_mode_key = None
        self._last_ptt = None
        self._last_full_resync_at = time.monotonic()
        # 0.0 ("long ago"), not now: a fresh attach must not be held back
        # by a gap measured against a write to the page that no longer
        # exists -- exact counterpart of _last_rig_write_at below.
        self._last_websdr_write_at = 0.0
        self._forward_freq_backoff.reset()
        self._forward_mode_backoff.reset()
        self._last_pushed_to_websdr_freq = None
        self._last_pushed_to_websdr_mode = None
        # "Just happened" rather than 0.0 -- a fresh attach gets the same
        # hold-off grace period a real forward push would, covering the
        # moment right after attach when the driver may still be settling.
        self._forward_push_completed_at = time.monotonic()
        self._reverse_reseed_due = False
        self._last_observed_freq = None
        self._pending_reverse_freq = None
        self._pending_reverse_freq_since = 0.0
        self._last_observed_mode_key = None
        self._reverse_baseline_captured = False
        self._last_unmapped_reverse_mode = None
        self._mode_push = None
        self._freq_push = None
        self._last_rig_write_at = 0.0
        self._pending_reverse_mode = None
        self._pending_reverse_mode_since = 0.0
        self._reverse_sync_error = None
        self._sync_latch_generation += 1
        # The forward side's own guard -- see _forward_latch_generation.
        # Bumped here and ONLY here.
        self._forward_latch_generation += 1

    async def _poll_loop(self) -> None:
        logger.info("Sync engine started (bidirectional: rig <-> WebSDR)")
        while not self.stop_event.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("Unexpected error in sync loop tick")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self.settings.poll_interval_s)
            except asyncio.TimeoutError:
                pass

    async def _reverse_sync_tick(self, websdr_status: WebSDRStatus, state: RigState) -> None:
        """WebSDR -> rig (v11). Called once per tick by _tick(), already
        gated by the caller (not transmitting, WebSDR connected, past
        REVERSE_HOLDOFF_S since the last successful push in either
        direction). At most one push
        (mode OR freq, mode taking priority) per tick, mirroring the
        forward direction's own mode-before-frequency ordering and
        bounding tick duration -- relevant especially for flrig, where
        get_state() is already 4 sequential XML-RPC round trips and
        set_mode/set_freq's own poll-until-match readback adds more.

        Guards every latch write after an await against a concurrent
        _reset_sync_latches() (run by _attach_supervisor on a different
        task on the same loop) by checking _sync_latch_generation --
        without this, a reset landing mid-await could have its fresh
        state overwritten by this call's now-stale success/failure
        bookkeeping once the await returns."""
        generation = self._sync_latch_generation
        obs_mode = self._driver.hamlib_mode_from_status(websdr_status)
        obs_freq = self._driver.rig_freq_from_status(websdr_status)

        if not self._reverse_baseline_captured or self._reverse_reseed_due:
            # First observation after WebSDR (re)attach/toggle-on, OR the
            # first observation after a forward push actually changed
            # something -- seed, don't push, in both cases.
            #
            # Attach/toggle-on case: the page may be sitting at its own
            # default (e.g. right after a forward push legitimately
            # failed, such as an out-of-band frequency) -- treating that
            # as a user action would yank the real rig onto a stranger's
            # WebSDR site's default.
            #
            # Post-forward-push case: _last_pushed_to_websdr_mode stores
            # the rig-NATIVE mode name (e.g. "PKTUSB"), but obs_mode is
            # always the canonical, WebSDR-normalized form (e.g. "USB")
            # -- for every many-to-one forward mapping those two strings
            # can never compare equal, so a straight equality check
            # alone would misread the forward push's own echo as a fresh
            # user edit and bounce a collapsed-but-different mode back
            # to the rig. Re-seeding from whatever the page now shows
            # (once the hold-off has given it time to catch up) adopts
            # the post-push reality as the new known-good baseline
            # without needing each driver's forward-collapse table at
            # the engine level.
            self._last_observed_freq = obs_freq
            self._last_observed_mode_key = obs_mode
            self._last_pushed_to_websdr_freq = obs_freq
            self._last_pushed_to_websdr_mode = obs_mode
            self._reverse_baseline_captured = True
            self._reverse_reseed_due = False
            return

        # See REVERSE_MIN_RIG_WRITE_GAP_S -- computed once per tick and
        # applied to both axes below, since a blocked mode write must not
        # let the freq branch write in the same tick either (that would
        # defeat the point of a *global* floor).
        can_write_rig = time.monotonic() - self._last_rig_write_at >= REVERSE_MIN_RIG_WRITE_GAP_S

        mode_did_work = False
        if obs_mode is None:
            # Unmapped WebSDR mode (e.g. KiwiSDR's sal/sau/sas/qam/drm/iq,
            # or an OpenWebRX digital mode) -- rate-limited, so a user
            # parked on an unmapped mode doesn't spam the log every tick.
            if websdr_status.mode != self._last_unmapped_reverse_mode:
                self._last_unmapped_reverse_mode = websdr_status.mode
                logger.debug(
                    "WebSDR mode %r has no reverse hamlib mapping; frequency reverse-sync continues",
                    websdr_status.mode,
                )
        else:
            # The page moved on to a different mode while a push was
            # still being retried (most often rapid clicking through
            # several modes) -- the in-flight target is stale. Drop it
            # rather than keep chasing a value the user already clicked
            # past; this is also what prevents a stale rejection warning
            # from an abandoned intermediate click lingering on screen
            # after the user has already moved on to something else.
            if self._mode_push is not None and obs_mode != self._mode_push.target:
                self._mode_push = None

            if obs_mode != self._pending_reverse_mode:
                self._pending_reverse_mode = obs_mode
                self._pending_reverse_mode_since = time.monotonic()

            if obs_mode != self._last_pushed_to_websdr_mode and obs_mode != self._last_observed_mode_key:
                if self._mode_push is None:
                    if time.monotonic() - self._pending_reverse_mode_since >= REVERSE_MODE_DEBOUNCE_S:
                        self._mode_push = _ReversePush(target=obs_mode)
                push = self._mode_push
                if push is not None and can_write_rig and time.monotonic() >= push.next_attempt_at:
                    # CW is the one mode family where multiple rig-native
                    # names (CW, CWR, CW-U, CW-L) can collapse to the same
                    # WebSDR-observable value ("CW") -- the page has no way
                    # to tell us which variant it wants, for a driver whose
                    # OWN reverse map is that ambiguous (self._driver.
                    # CW_VARIANT_IS_AMBIGUOUS -- true for websdr.org/
                    # KiwiSDR/OpenWebRX, false for UberSDR, which keeps
                    # cwu/cwl -- and so CW/CWR -- distinct both ways; see
                    # each driver's own flag for why). Some rigctld
                    # backends/rig models only accept "CW-U"/"CW-L" as valid
                    # SET-mode strings and reject a bare "CW" outright
                    # (confirmed live: KiwiSDR + a rig with no plain CW
                    # mode). So when the rig is ALREADY in some CW-family
                    # mode AND the observation is genuinely ambiguous, echo
                    # the rig's own current name back instead of forcing it
                    # to "CW" -- but only then: for an unambiguous driver,
                    # obs_mode already names one specific rig-native mode,
                    # and echoing the rig's current (possibly DIFFERENT)
                    # CW-family name back would silently override a real
                    # page click (e.g. UberSDR cwl->cwu confirmed live to
                    # revert to the rig's stale CWR ~30s later). A genuine
                    # transition INTO CW from a different mode family falls
                    # back to the canonical obs_mode either way, since
                    # there's no rig-native value to echo in that case.
                    mode_to_send = obs_mode
                    if (
                        obs_mode == "CW"
                        and self._driver.CW_VARIANT_IS_AMBIGUOUS
                        and state.mode is not None
                        and state.mode.upper().startswith("CW")
                    ):
                        mode_to_send = state.mode
                    elif self.settings.force_ssb_to_data_mode:
                        # "Force SSB to data mode" (Behaviour settings) --
                        # send the rig's own data-mode token instead of
                        # plain USB/LSB. Bookkeeping below stays keyed on
                        # the canonical obs_mode ("USB"/"LSB"), same
                        # pattern as the CW echo above: the WebSDR page
                        # itself is still (and should still be compared
                        # as) plain USB/LSB, only the string actually
                        # written to the rig differs.
                        mode_to_send = _reverse_sync_data_mode_for(obs_mode, self._rig_backend)
                    # Mode-only -- filter/bandwidth is never touched by a
                    # reverse-sync push (see RigClient.set_mode()'s own
                    # docstring: a user reported live that any reverse-sync
                    # filter change was unwelcome, even a "sensible" one).
                    ok = await self._rig.set_mode(mode_to_send, verify_budget_s=REVERSE_PUSH_ATTEMPT_VERIFY_S)
                    # Stamp unconditionally, before the generation guard --
                    # the write physically happened on the wire regardless
                    # of whether a concurrent reset superseded our own
                    # in-memory bookkeeping while we were awaiting it.
                    self._last_rig_write_at = time.monotonic()
                    if generation != self._sync_latch_generation:
                        return  # superseded by a concurrent reset while awaiting
                    mode_did_work = True
                    push.attempts += 1
                    if ok:
                        self._last_pushed_to_websdr_mode = obs_mode
                        self._last_observed_mode_key = obs_mode
                        self._forward_push_completed_at = time.monotonic()
                        self._reverse_sync_error = None
                        self._mode_push = None
                    elif push.attempts >= REVERSE_PUSH_MAX_ATTEMPTS:
                        # Gave it several tries, spaced out, not just one
                        # shot -- genuinely not landing. Rig status is
                        # master: rather than leave the two sides silently
                        # disagreeing forever, revert the WebSDR page to
                        # match the rig's actual mode (force the forward
                        # direction to re-assert it on the very next tick)
                        # instead of waiting indefinitely for a click that
                        # isn't sticking.
                        # User-visible text names mode_to_send, the string
                        # actually written to the rig, not the canonical
                        # obs_mode -- with the CW echo above those differ
                        # (page says "CW", the rig was sent "CW-L"), and
                        # naming a value that was never sent misleads
                        # anyone diagnosing a real rejection. Internal
                        # bookkeeping below stays keyed on obs_mode.
                        logger.warning(
                            "Rig did not confirm mode %r after %d attempts -- "
                            "reverting WebSDR to match the rig's actual mode %r",
                            mode_to_send, push.attempts, state.mode,
                        )
                        self._reverse_sync_error = (
                            f"Could not set mode {mode_to_send!r} on the rig -- WebSDR reverted to match the rig"
                        )
                        self._last_observed_mode_key = obs_mode
                        self._last_sent_mode_key = None
                        self._mode_push = None
                    else:
                        push.next_attempt_at = time.monotonic() + REVERSE_PUSH_BACKOFF_S[
                            min(push.attempts - 1, len(REVERSE_PUSH_BACKOFF_S) - 1)
                        ]
            else:
                self._last_observed_mode_key = obs_mode
                self._mode_push = None

        if mode_did_work:
            return  # per-tick budget cap: mode takes priority over frequency this tick

        if obs_freq is None:
            return

        now = time.monotonic()
        if (
            self._pending_reverse_freq is None
            or abs(obs_freq - self._pending_reverse_freq) > REVERSE_FREQ_CHANGE_THRESHOLD_HZ
        ):
            self._pending_reverse_freq = obs_freq
            self._pending_reverse_freq_since = now
            return
        if now - self._pending_reverse_freq_since < REVERSE_FREQ_DEBOUNCE_S:
            return
        if (
            self._last_pushed_to_websdr_freq is not None
            and abs(self._pending_reverse_freq - self._last_pushed_to_websdr_freq) <= REVERSE_FREQ_CHANGE_THRESHOLD_HZ
        ) or (
            self._last_observed_freq is not None
            and abs(self._pending_reverse_freq - self._last_observed_freq) <= REVERSE_FREQ_CHANGE_THRESHOLD_HZ
        ):
            # Mirrors the mode branch's identical "nothing to do" refresh
            # (the `else` at the bottom of the mode block above) -- without
            # this, _last_observed_freq freezes at whatever it was last set
            # to (typically the reseed value) and never tracks reality
            # again, so a frequency that happens to match that frozen value
            # gets silently treated as "already accounted for" and dropped,
            # even after the rig has since moved somewhere else entirely.
            self._last_observed_freq = self._pending_reverse_freq
            self._freq_push = None
            return

        pending_freq = self._pending_reverse_freq
        min_hz = self.settings.reverse_sync_min_hz
        max_hz = self.settings.reverse_sync_max_hz
        if (min_hz is not None and pending_freq < min_hz) or (max_hz is not None and pending_freq > max_hz):
            # Reject immediately -- skip the retry ladder entirely, since
            # retrying can never make an out-of-range value pass. Same
            # rig-is-master revert the ladder's own give-up path uses
            # (force the forward direction to re-assert the rig's real
            # frequency on the next tick) rather than leaving the WebSDR
            # page sitting on an unreachable value.
            logger.warning(
                "Reverse-sync frequency %d Hz is outside the configured range [%s, %s] Hz -- "
                "rejected, reverting WebSDR to match the rig",
                pending_freq,
                min_hz if min_hz is not None else "-inf",
                max_hz if max_hz is not None else "+inf",
            )
            self._reverse_sync_error = (
                f"Frequency {pending_freq} Hz is outside the configured reverse-sync range -- "
                "WebSDR reverted to match the rig"
            )
            self._last_observed_freq = pending_freq
            self._last_sent_freq = None
            self._freq_push = None
            return
        if self._freq_push is None or self._freq_push.target != pending_freq:
            self._freq_push = _ReversePush(target=pending_freq)
        push = self._freq_push
        if not can_write_rig or now < push.next_attempt_at:
            return

        ok = await self._rig.set_freq(pending_freq, verify_budget_s=REVERSE_PUSH_ATTEMPT_VERIFY_S)
        # See the mode branch's identical comment -- stamp before the
        # generation guard, the write already happened either way.
        self._last_rig_write_at = time.monotonic()
        if generation != self._sync_latch_generation:
            return  # superseded by a concurrent reset while awaiting
        push.attempts += 1
        if ok:
            self._last_pushed_to_websdr_freq = pending_freq
            # See the mode branch's identical pair of assignments on its
            # own success path -- _last_observed_freq must stay current or
            # this exact frequency becomes permanently unpushable the next
            # time the page returns to it (see the dedupe-gate comment above).
            self._last_observed_freq = pending_freq
            self._forward_push_completed_at = time.monotonic()
            self._reverse_sync_error = None
            self._freq_push = None
        elif push.attempts >= REVERSE_PUSH_MAX_ATTEMPTS:
            logger.warning(
                "Rig did not confirm frequency %d Hz after %d attempts -- "
                "reverting WebSDR to match the rig's actual frequency %r Hz",
                pending_freq, push.attempts, state.freq_hz,
            )
            self._reverse_sync_error = (
                f"Could not set frequency {pending_freq} Hz on the rig -- WebSDR reverted to match the rig"
            )
            self._last_observed_freq = pending_freq
            self._last_sent_freq = None
            self._freq_push = None
        else:
            # time.monotonic() freshly, NOT the stale pre-await `now`:
            # a failing set_freq() blocks for ~REVERSE_PUSH_ATTEMPT_VERIFY_S
            # first, so now + backoff would already be in the past and the
            # inter-attempt gap would be a silent no-op (matches the mode
            # branch above).
            push.next_attempt_at = time.monotonic() + REVERSE_PUSH_BACKOFF_S[
                min(push.attempts - 1, len(REVERSE_PUSH_BACKOFF_S) - 1)
            ]

    async def _tick(self) -> None:
        """Runs one poll iteration. A no-op (beyond publishing a status
        snapshot) for whichever subsystem isn't active -- rig and WebSDR
        being independently startable means neither can assume the other
        is present."""
        # cw_offset_hz was previously only ever read at driver
        # construction time (see driver_cls(...) call sites above), so
        # changing it in Behaviour settings while already connected had
        # no effect until a disconnect/reconnect or site switch -- kept
        # the OLD value applied indefinitely.
        #
        # A blind per-tick overwrite (the first attempt at this fix) is
        # itself a real bug, caught by a bug-hunter review pass: the
        # offset also feeds the REVERSE direction (WebSDR -> rig, via
        # each driver's rig_freq_from_status()/_reverse_effective_hz()),
        # so changing it mid-session shifts what "the page's current
        # frequency" is read AS, with the page itself not having moved at
        # all -- _reverse_sync_tick() then sees a phantom jump of exactly
        # the offset delta and, with REVERSE_FREQ_CHANGE_THRESHOLD_HZ=0,
        # pushes it to the rig as if the operator had clicked the page.
        # Reproduced live: editing the spinner alone retuned the rig by
        # the offset delta. Only sync a GENUINE change (compared against
        # what was last actually applied, not re-detected every tick from
        # a value that never moves), and treat it the same way a
        # mode-driven effective-frequency shift already is elsewhere in
        # this method: force a forward re-push (_last_sent_freq = None)
        # and a reverse re-baseline (_reverse_reseed_due = True) instead
        # of letting the shift be misread as a page edit.
        if self._driver is not None and self.settings.cw_offset_hz != self._applied_cw_offset_hz:
            self._driver.cw_offset_hz = self.settings.cw_offset_hz
            self._applied_cw_offset_hz = self.settings.cw_offset_hz
            self._last_sent_freq = None
            self._reverse_reseed_due = True

        # auto_click_audio_unlock has the identical staleness shape
        # cw_offset_hz had -- same bug-hunter finding -- but no reverse-
        # sync side effect (it only gates a background overlay-clicking
        # watcher, doesn't feed any frequency/mode mapping), so a blind
        # per-tick overwrite is safe here, unlike cw_offset_hz above.
        # Each driver's watcher loop (kiwisdr.py/openwebrx.py) re-checks
        # this attribute every iteration, so a live change actually
        # stands an already-running watcher down instead of only taking
        # effect on the next reconnect.
        if self._driver is not None:
            self._driver._auto_click_audio_unlock = self.settings.auto_click_audio_unlock

        # NOT fetched unconditionally up front any more -- a bug-hunter
        # review pass found this call was ALWAYS made (one full WebView
        # JS round-trip) but only ever actually used by the three
        # early-return branches below (rig inactive/still-connecting/
        # nulled-mid-tick). In the steady state (rig connected, nothing
        # to early-return for) it was computed and immediately discarded,
        # doubling get_status()'s real per-tick page-poll cost for
        # nothing -- confirmed live: 2 calls/tick instead of 1. Fetched
        # lazily now, once per branch that actually needs it, at the same
        # point in the sequence the single eager fetch used to occupy
        # (still BEFORE that branch's own _idle_disconnect_if_due() call,
        # so a session it tears down mid-branch is still described by the
        # status captured just before teardown, same as before).
        async def _websdr_status_now() -> Optional[WebSDRStatus]:
            return await self._driver.get_status() if self._websdr_active and self._driver else None

        if not self._rig_active or self._rig is None:
            self._rig_was_connected_last_tick = False
            websdr_status = await _websdr_status_now()
            if await self._idle_disconnect_if_due():
                # The session described by websdr_status was just torn
                # down -- don't publish a snapshot describing it (same
                # reasoning as the give-up path below).
                websdr_status = None
            self._publish(rig_connected=False, websdr=websdr_status)
            return

        if not await self._rig.ensure_connected():
            self._rig_was_connected_last_tick = False
            if self._rig_connect_deadline is not None and time.monotonic() > self._rig_connect_deadline:
                give_up_message = (
                    f"Could not connect to {self._rig_backend} at {self._rig_host}:{self._rig_port} "
                    f"within {RIG_CONNECT_TIMEOUT_S:.0f}s, giving up"
                )
                logger.warning(give_up_message)
                # _stop_rig() publishes its own fresh snapshot (reflecting
                # rig_active=False and, via its cascade, websdr_active=False
                # too) -- don't also publish a websdr_status describing a
                # session that's about to be torn down.
                await self._stop_rig(error=give_up_message)
                return
            # The ordinary still-trying-to-connect path -- note this is
            # also the path a rig that drops AFTER connecting once takes
            # forever, since _rig_connect_deadline is cleared on first
            # success and the give-up branch above can never fire again.
            # That is the realistic unattended failure case, and it must
            # not hold a public receiver's audio slot indefinitely.
            websdr_status = await _websdr_status_now()
            if await self._idle_disconnect_if_due():
                websdr_status = None
            self._publish(rig_connected=False, websdr=websdr_status)
            return

        self._rig_connect_deadline = None
        # See _rig_was_connected_last_tick: the not-connected -> connected
        # edge is itself a "something happened at the radio" signal and
        # must count as activity even when nothing about the reported
        # state changed across the outage.
        rig_reconnected = not self._rig_was_connected_last_tick
        self._rig_was_connected_last_tick = True
        if rig_reconnected:
            self._last_rig_activity_at = time.monotonic()
        if self._rig is None:
            # A concurrent Disconnect (_stop_rig(), which cascades to
            # _stop_websdr() too) can null this while the ensure_connected()
            # await above was suspended -- there is no rig state left to
            # read this tick. Same not-connected publish/return as the
            # guard at the top of this method for the identical condition.
            self._rig_was_connected_last_tick = False
            websdr_status = await _websdr_status_now()
            if await self._idle_disconnect_if_due():
                websdr_status = None
            self._publish(rig_connected=False, websdr=websdr_status)
            return
        state = await self._rig.get_state()
        # Diagnostic only (v2.2.2) -- see _last_logged_rig_freq's docstring.
        if state.freq_hz is not None and state.freq_hz != self._last_logged_rig_freq:
            logger.info("rig freq_hz read: %d (was %s)", state.freq_hz, self._last_logged_rig_freq)
            self._last_logged_rig_freq = state.freq_hz

        # v14 idle tracking. Runs on EVERY tick the rig is connected,
        # whether or not a WebSDR session exists, so idle time keeps
        # accumulating (and resetting) while disconnected -- that's what
        # makes auto-resume able to fire on the very tick the operator
        # touches the rig again.
        rig_activity = self._note_rig_activity(state)
        if rig_reconnected:
            rig_activity = True  # unconditionally, see above
        if self._websdr_idle_stopped and rig_activity:
            await self._resume_websdr_after_idle()
        elif await self._idle_disconnect_if_due():
            self._publish(rig_connected=True, rig_freq_hz=state.freq_hz, rig_mode=state.mode, rig_ptt=state.ptt)
            return

        if self._websdr_active and self._driver is not None:
            # Captured before any awaits below, for the reverse gate's
            # own PTT check further down -- see its comment for why.
            last_ptt_before_awaits = self._last_ptt

            # See WEBSDR_MIN_WRITE_GAP_S -- computed ONCE per tick, before
            # anything that writes, and applied uniformly to both
            # forward-push axes below. Mute is NOT gated by this any more
            # (v15): it's a native WebView-level call (see
            # browser_shim.py's set_muted()), not a page write, so there's
            # nothing to rate-limit -- the whole point of the native path
            # is that it doesn't share the page's own write cadence at
            # all. Recomputing it per axis would make it two independent
            # floors instead of one global gate, which is not what a rate
            # limit is.
            can_write_websdr = time.monotonic() - self._last_websdr_write_at >= WEBSDR_MIN_WRITE_GAP_S

            # _last_ptt is deliberately NOT latched unless self._page
            # actually exists: a PTT edge arriving with no page to mute
            # must still fire once one exists again, or an unmute could
            # be lost for the rest of the session (the same failure mode
            # the falling-edge comment below already guards against).
            if state.ptt is not None and state.ptt != self._last_ptt and self._page is not None:
                self._last_ptt = state.ptt
                if state.ptt:
                    if self.settings.mute_on_tx:
                        await self._page.set_muted(True)
                else:
                    # Always unmute on the falling edge, regardless of the
                    # *current* mute_on_tx value -- if the user unchecks
                    # mute_on_tx while still muted from an earlier TX, the
                    # old gate-both-directions logic would skip this call
                    # (since it also checked mute_on_tx here), leaving the
                    # WebSDR muted indefinitely with no further edge ever
                    # able to clear it. Unmuting when not actually muted
                    # is a harmless no-op.
                    await self._page.set_muted(False)

            transmitting = bool(self._last_ptt)

            if not transmitting:
                now = time.monotonic()
                # Captured once, before any push, and re-checked after
                # every await below -- _reset_sync_latches() can run from
                # the attach supervisor's task on this same loop while a
                # push is in flight (_handle_page_dead -> _start_websdr ->
                # _reset_sync_latches is a real, reachable path), and a
                # stale write from the DEAD page would otherwise mark the
                # freshly-reattached one as already up to date and
                # suppress its own re-push. Exactly the guard
                # _reverse_sync_tick() has always had; the forward
                # direction was missing it.
                forward_generation = self._forward_latch_generation
                # See FULL_RESYNC_INTERVAL_S -- forces both pushes below
                # through regardless of whether the dedupe latches think
                # they're already up to date, as a periodic safety net
                # against the WebSDR page and the rig silently drifting
                # out of agreement with nothing noticing.
                due_for_periodic_resync = now - self._last_full_resync_at >= FULL_RESYNC_INTERVAL_S
                # Item 3 bookkeeping: a resync cycle only counts as done
                # if something was actually sent and nothing eligible was
                # held back -- see the stamp at the end of this block.
                resync_push_attempted = False
                resync_push_blocked = False
                # Set when a push's result is discarded because a
                # concurrent _reset_sync_latches() superseded it. Skips
                # the REST of this forward block outright, mirroring
                # _reverse_sync_tick()'s return-on-mismatch. Previously
                # the mode branch just fell through, which happened to be
                # safe only because _reset_sync_latches() also clears
                # _pending_freq and that incidentally routed the freq
                # branch into its re-arm-debounce path -- an undocumented,
                # untested coupling between two distant pieces of code.
                forward_superseded = False

                mode_key = (state.mode, state.passband_hz) if state.mode is not None else None
                # Suppress the forward push while a reverse-sync mode
                # ladder is actively retrying -- the rig still reports its
                # OLD mode during that window, and pushing it back onto
                # the WebSDR page would change obs_mode, which would then
                # look like the user abandoned their own click and
                # supersede the very push being retried.
                if (
                    not self._forward_sync_paused
                    and mode_key is not None
                    and (mode_key != self._last_sent_mode_key or due_for_periodic_resync)
                    and self._mode_push is None
                ):
                    backoff = self._forward_mode_backoff
                    backoff.note_target(mode_key)
                    if can_write_websdr and time.monotonic() >= backoff.next_attempt_at:
                        # Only latch as "sent" if the driver actually applied it
                        # -- it may have been a no-op (not attached, e.g.
                        # mid-outage) or a failed page call, and either would
                        # otherwise be wrongly recorded as delivered, silencing
                        # all further retries even after the WebSDR recovers.
                        # self._driver can turn None here -- a concurrent
                        # _stop_websdr()/_switch_websdr() can run during one
                        # of the several real awaits earlier this same tick
                        # (get_state(), _idle_disconnect_if_due(),
                        # _resume_websdr_after_idle() -- NOT the PTT-mute
                        # call any more: WxPageAdapter.set_muted() is v15
                        # fire-and-forget, no await inside it, so it can't
                        # itself be the suspension point) -- so this is a
                        # genuine no-op, not just "not attached yet".
                        if self._driver is not None:
                            applied = await self._driver.set_mode(state.mode, state.passband_hz)
                        else:
                            applied = False
                        # Stamped unconditionally, before the generation
                        # guard, for the same reason the reverse side does
                        # it: the command physically went to the far end
                        # regardless of what we decide about the result.
                        self._last_websdr_write_at = time.monotonic()
                        resync_push_attempted = True
                        if forward_generation != self._forward_latch_generation:
                            # Superseded by a concurrent reset (the page we
                            # wrote to is gone) -- drop the whole result,
                            # bookkeeping and ladder alike, and skip the
                            # rest of this block.
                            forward_superseded = True
                        elif applied:
                            backoff.record_success()
                            # Captured BEFORE the reassignment just below --
                            # True only when this push actually changed the
                            # rig's mode, as opposed to firing purely because
                            # due_for_periodic_resync forced an unconditional
                            # re-send of an ALREADY-agreed value. Only a real
                            # change needs the reseed below: the WebSDR page
                            # already shows the correct canonical form of an
                            # unchanged mode from the last time it genuinely
                            # changed, so there is nothing new to bridge.
                            # Arming the reseed on every resync regardless
                            # opened a ~REVERSE_HOLDOFF_S window, every
                            # ~FULL_RESYNC_INTERVAL_S, where a genuine page
                            # click landing in it got silently adopted as the
                            # new "baseline" (not pushed to the rig) instead
                            # of triggering a real reverse push -- confirmed
                            # live, see the regression test below.
                            mode_actually_changed = mode_key != self._last_sent_mode_key
                            self._last_sent_mode_key = mode_key
                            # A mode change can change the effective frequency
                            # sent to the WebSDR (e.g. CW offset), so force a
                            # re-push even if the raw rig frequency itself
                            # didn't move.
                            self._last_sent_freq = None
                            self._last_pushed_to_websdr_mode = state.mode
                            self._forward_push_completed_at = time.monotonic()
                            if mode_actually_changed:
                                self._reverse_reseed_due = True
                        else:
                            backoff.record_failure()
                    elif not can_write_websdr:
                        # ONLY the global write gap counts as "held back"
                        # for the resync stamp. An axis sitting in its own
                        # failure ladder has an independent retry
                        # schedule and must not hold the whole periodic
                        # resync cycle hostage: withholding the stamp for
                        # it leaves due_for_periodic_resync permanently
                        # True, which forces the OTHER, healthy axis past
                        # its dedupe latch on every single tick, bounded
                        # only by the 0.5s write gap. That is traffic
                        # amplification (simulated: 255 writes where 10
                        # were intended), currently masked only by
                        # FORWARD_PUSH_BACKOFF_MAX_S and
                        # FULL_RESYNC_INTERVAL_S both happening to be
                        # 30.0 -- an invisible coupling between two
                        # constants that have no reason to move together.
                        resync_push_blocked = True

                if state.freq_hz is not None and not forward_superseded:
                    if self._pending_freq is None or abs(state.freq_hz - self._pending_freq) > FREQ_CHANGE_THRESHOLD_HZ:
                        # Diagnostic only (v2.2.2) -- see _last_logged_rig_freq.
                        if self._pending_freq is not None and state.freq_hz != self._pending_freq:
                            logger.info(
                                "forward freq candidate -> %d Hz (was %d, debounce restarted)",
                                state.freq_hz, self._pending_freq,
                            )
                        self._pending_freq = state.freq_hz
                        self._pending_freq_since = now
                    elif (
                        not self._forward_sync_paused
                        and now - self._pending_freq_since >= FREQ_DEBOUNCE_S
                        and (self._last_sent_freq is None
                             or abs(self._pending_freq - self._last_sent_freq) > FREQ_CHANGE_THRESHOLD_HZ
                             or due_for_periodic_resync)
                        and self._freq_push is None  # see the mode ladder's identical guard above
                    ):
                        # freq_changed mirrors the disjuncts above exactly
                        # (no await in between, so they can't disagree).
                        freq_changed = (
                            self._last_sent_freq is None
                            or abs(self._pending_freq - self._last_sent_freq) > FREQ_CHANGE_THRESHOLD_HZ
                        )
                        # Diagnostic only (v2.2.2) -- see _last_logged_rig_freq.
                        logger.info(
                            "forward freq debounce satisfied: pending=%d last_sent=%s freq_changed=%s "
                            "can_write_websdr=%s",
                            self._pending_freq, self._last_sent_freq, freq_changed, can_write_websdr,
                        )
                        # verify=False ONLY for a periodic-resync re-push
                        # of an unchanged frequency WHILE a reverse-sync
                        # push is actively in flight -- that's the one
                        # window where an armed corrective re-tune 0.6s
                        # later (see WebSDRDriver.tune_hz's docstring) can
                        # fight a genuine, concurrent reverse-sync push.
                        # Outside that window, an unchanged-value resync
                        # still verifies -- it's the only thing that ever
                        # notices/repairs the page's band selection having
                        # drifted from our own tracking (e.g. a direct
                        # click on the page's own band control while PTT
                        # suppressed reverse sync), and disabling it
                        # unconditionally would silently give that up.
                        # Only _mode_push is tested here: the enclosing
                        # elif above already requires self._freq_push is
                        # None for this code to run at all (and nothing
                        # awaits in between, so it can't change), so
                        # re-testing it here would be unconditionally
                        # True -- don't "fix" this by adding it back.
                        verify = freq_changed or self._mode_push is None
                        backoff = self._forward_freq_backoff
                        backoff.note_target(self._pending_freq)
                        if can_write_websdr and time.monotonic() >= backoff.next_attempt_at:
                            # Diagnostic only (v2.2.2) -- see _last_logged_rig_freq.
                            logger.info("forward freq push: attempting %d Hz (verify=%s)", self._pending_freq, verify)
                            # See the mode branch's identical comment: a
                            # concurrent stop/switch can null self._driver
                            # during an earlier await this same tick.
                            if self._driver is not None:
                                applied = await self._driver.tune_hz(self._pending_freq, verify=verify)
                            else:
                                applied = False
                            self._last_websdr_write_at = time.monotonic()  # see the mode branch
                            resync_push_attempted = True
                            if forward_generation != self._forward_latch_generation:
                                forward_superseded = True  # concurrent reset while awaiting
                                logger.info("forward freq push: superseded by a concurrent reset")
                            elif applied:
                                backoff.record_success()
                                # Captured BEFORE the reassignment just below,
                                # and deliberately NOT freq_changed (computed
                                # above from _last_sent_freq): the mode branch
                                # earlier THIS SAME tick unconditionally resets
                                # _last_sent_freq to None on every successful
                                # mode push, including a due_for_periodic_resync
                                # no-op one -- so freq_changed reads True there
                                # regardless of whether the frequency actually
                                # moved (a false positive that defeated an
                                # earlier version of this fix, caught by the
                                # regression test below actually running it).
                                # _last_pushed_to_websdr_freq is untouched by
                                # that reset, so it reflects the real prior
                                # value.
                                freq_actually_changed = self._pending_freq != self._last_pushed_to_websdr_freq
                                self._last_sent_freq = self._pending_freq
                                self._last_pushed_to_websdr_freq = self._pending_freq
                                self._forward_push_completed_at = time.monotonic()
                                if freq_actually_changed:
                                    self._reverse_reseed_due = True
                                logger.info("forward freq push: applied %d Hz", self._pending_freq)
                            else:
                                backoff.record_failure()
                                logger.info("forward freq push: driver.tune_hz(%d) returned False", self._pending_freq)
                        elif not can_write_websdr:
                            resync_push_blocked = True  # write gap only -- see the mode branch
                            # Diagnostic only (v2.2.2) -- see _last_logged_rig_freq.
                            logger.info(
                                "forward freq push: blocked by write gap or backoff (pending=%d, "
                                "next_attempt_at in %.2fs)",
                                self._pending_freq, backoff.next_attempt_at - time.monotonic(),
                            )

                # Stamped ONLY when this cycle's push(es) actually reached
                # the driver and nothing eligible was held back. Stamping
                # it unconditionally (the pre-v14 behavior) was harmless
                # while every scheduled push always ran, but now that the
                # write gap and the failure ladders can skip one, it would
                # record a resync that never happened and push the real
                # safety net a further FULL_RESYNC_INTERVAL_S into the
                # future every time -- silently disabling the one thing
                # that repairs a persistent desync. Leaving it unstamped
                # means the next tick retries as soon as the gate clears.
                if due_for_periodic_resync and resync_push_attempted and not resync_push_blocked:
                    if not forward_superseded:
                        self._last_full_resync_at = now

            if self._driver is not None:
                websdr_status = await self._driver.get_status()
            else:
                # Torn down mid-tick by a concurrent stop/switch (see the
                # mode/freq branches' identical comments) -- nothing left
                # to read or reverse-sync against this tick. The
                # "if not self._websdr_active" guard below the whole block
                # already re-nulls websdr_status for the outer publish
                # either way; this just stops the read from crashing first.
                websdr_status = None

            # v11 reverse sync (WebSDR -> rig). Prefer this tick's fresh
            # state.ptt over self._last_ptt -- _reset_sync_latches() can
            # run from a different task (the attach supervisor)
            # concurrently with an in-flight _tick() and clear _last_ptt
            # mid-tick in a narrow window, which state.ptt has no such
            # cross-task dependency on. But state.ptt can also be None
            # (a transient read failure) -- treating that as "not
            # transmitting" would let reverse sync retune mid-TX on a
            # single dropped PTT poll, defeating the whole point of this
            # gate. Fall back to the PTT value already known before this
            # tick's awaits (last_ptt_before_awaits, captured above)
            # rather than either assuming safe or re-reading
            # self._last_ptt post-await.
            reverse_ptt = state.ptt if state.ptt is not None else last_ptt_before_awaits
            if (
                websdr_status is not None
                # self._driver can have gone None WHILE the get_status()
                # await above was suspended (a concurrent _stop_websdr()/
                # _stop_rig() completing mid-await) even though that call
                # itself still returned a real, usable websdr_status --
                # _reverse_sync_tick() dereferences self._driver
                # immediately, with no guard of its own.
                and self._driver is not None
                and not reverse_ptt
                and websdr_status.connected
                and not self._reverse_sync_held
                and time.monotonic() - self._forward_push_completed_at >= REVERSE_HOLDOFF_S
            ):
                await self._reverse_sync_tick(websdr_status, state)

        if not self._websdr_active:
            # websdr_status may describe a session that a concurrent
            # stop_websdr_from_other_thread()/_stop_rig() cascade tore down
            # while this tick was mid-flight (this method has several
            # awaits above, any of which can yield to that coroutine on
            # the same loop) -- same reasoning as the two websdr_status =
            # None guards earlier in this method, just reached from the
            # "rig fully connected" path instead of the "not yet
            # connected" ones. Without this, _publish() below reads
            # websdr_active=False fresh from self but would still attach
            # this stale non-None status, which looks exactly like a
            # genuine attach failure to _apply_snapshot()'s
            # _websdr_connect_pending guard and tears the GUI's
            # just-started reconnect back down.
            websdr_status = None
        self._publish(
            rig_connected=True,
            rig_freq_hz=state.freq_hz,
            rig_mode=state.mode,
            rig_ptt=state.ptt,
            websdr=websdr_status,
        )


def _format_duration(seconds: float) -> str:
    """Human-facing, for status text only -- seconds below ~1.5 minutes,
    whole minutes above that (nobody reading 'still retrying' cares about
    the seconds once it's been minutes)."""
    if seconds < 90:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.0f} min"


def _describe_webview_create_error(e: Exception) -> str:
    return f"Could not create the WebSDR browser view: {e}"
