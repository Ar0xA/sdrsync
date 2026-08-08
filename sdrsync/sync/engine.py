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
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from sdrsync.config import AppSettings, WebSDRSite
from sdrsync.gui_messages import GuiMessage
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
FREQ_CHANGE_THRESHOLD_HZ = 10  # ignore rig jitter smaller than this
ATTACH_RETRY_BASE_DELAY_S = 2.0
ATTACH_RETRY_MAX_DELAY_S = 30.0
ATTACH_CHECK_INTERVAL_S = 1.0  # how often to notice the driver dropped attachment
# How long to keep retrying an initial rig connection before giving up and
# surfacing a real error instead of leaving the GUI stuck at "connecting..."
# forever. Applies to the time-to-FIRST-connect only -- cleared once
# ensure_connected() succeeds once, so a later transient drop-and-reconnect
# isn't bound by this same budget (see _tick()/_start_rig()).
RIG_CONNECT_TIMEOUT_S = 30.0

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
# Coarser than the forward direction's FREQ_CHANGE_THRESHOLD_HZ -- a
# waterfall click readback isn't pixel-exact.
REVERSE_FREQ_CHANGE_THRESHOLD_HZ = 50


class WebViewHost(Protocol):
    """The narrow interface SyncEngine needs from whatever manages the
    actual browser widget -- deliberately NOT importing wx or
    gui/webview_host.py here, so this module (and tests against it) never
    need a real wx.App. gui/webview_host.py's WebViewHost class satisfies
    this structurally; engine-level tests can supply any stub that does."""

    async def create_page(
        self, loop: "asyncio.AbstractEventLoop", on_dead: Optional[Callable[[str], None]] = None
    ) -> Page: ...

    async def destroy_page(self, page: Page) -> None: ...


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

    async def set_freq(self, freq_hz: int) -> bool: ...

    async def set_mode(self, mode_name: str, passband_hz: Optional[int]) -> bool: ...


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
    websdr_active: bool = False
    websdr: Optional[WebSDRStatus] = None
    fatal_error: Optional[str] = None
    # v11 reverse sync (WebSDR -> rig): set when the rig rejects a value
    # pushed from the WebSDR page, cleared on the next successful reverse
    # push or fresh WebSDR attach. No retry loop of its own -- a rejected
    # SET naturally gets another chance the next time the page changes.
    reverse_sync_error: Optional[str] = None


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

        # Sync dedupe latches -- reset whenever either subsystem
        # (re)starts, since "already sent to X" is meaningless once X has
        # changed out from under them.
        self._last_sent_freq: Optional[int] = None
        self._pending_freq: Optional[int] = None
        self._pending_freq_since: float = 0.0
        self._last_sent_mode_key: Optional[tuple[str, Optional[int]]] = None
        self._last_ptt: Optional[bool] = None
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
        self._reverse_reseed_due: bool = False

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
        # Rate-limits repeated-identical-rejection logging, same pattern
        # as each driver's own _last_unmapped_mode. Separate keys since a
        # mode rejection and a frequency rejection are unrelated events.
        self._last_reverse_mode_reject_key: Optional[str] = None
        self._last_reverse_freq_reject_key: Optional[int] = None
        self._reverse_sync_error: Optional[str] = None
        # Bumped by _reset_sync_latches() -- _reverse_sync_tick() checks
        # this after each await (set_mode()/set_freq() to the rig) before
        # writing any latch, since _attach_supervisor can call
        # _reset_sync_latches() from a different task on the same loop
        # while that await is in flight; without this guard, the
        # post-await write would resurrect pre-reattach bookkeeping over
        # a reset that already superseded it.
        self._sync_latch_generation: int = 0

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
        """Also used to *switch* sites: if a WebSDR session is already
        active, _start_websdr() replaces it -- there is no separate "switch"
        code path, loading a site always means "this is what's active now"."""
        if self._loop is None:
            return
        self._run_coro_threadsafe(self._start_websdr(site))

    def stop_websdr_from_other_thread(self) -> None:
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
        snapshot = StatusSnapshot(
            rig_active=self._rig_active,
            rig_error=self._rig_error,
            websdr_active=self._websdr_active,
            reverse_sync_error=self._reverse_sync_error,
            **overrides,
        )
        try:
            self.status_queue.put_nowait(snapshot)
        except queue.Full:
            pass

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
        self._rig_error = error
        self._rig_connect_deadline = None
        if was_active and error is None:
            logger.info("Rig session stopped")
        self._publish()

    # ------------------------------------------------------------------ websdr
    async def _start_websdr(self, site: WebSDRSite) -> None:
        driver_cls = DRIVERS.get(site.driver_type)
        if driver_cls is None:
            logger.error("No WebSDR driver registered for type %r", site.driver_type)
            self._publish(websdr=WebSDRStatus(
                connected=False, last_error=f"No WebSDR driver registered for type {site.driver_type!r}"
            ))
            return

        if self._websdr_active:
            await self._stop_websdr()

        self._websdr_generation += 1
        my_generation = self._websdr_generation

        try:
            page = await self._webview_host.create_page(
                self._loop, on_dead=lambda reason: self._on_page_dead(my_generation, reason)
            )
        except Exception as e:
            logger.exception("Failed to create WebView for WebSDR session")
            self._publish(websdr=WebSDRStatus(connected=False, last_error=_describe_webview_create_error(e)))
            return

        self._page = page
        self.site = site
        self._driver = driver_cls(site.url, cw_offset_hz=self.settings.cw_offset_hz)
        self._websdr_active = True
        self._reset_sync_latches()
        # Runs for the WebSDR subsystem's whole lifetime, independently of
        # the poll loop: (re)attaches whenever the driver isn't attached,
        # with backoff on failure.
        self._attach_task = asyncio.ensure_future(self._attach_supervisor(self._page))
        logger.info("WebSDR session started: %s (%s)", site.name, site.url)
        self._publish()

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
        asyncio.ensure_future(self._start_websdr(self.site))

    async def _stop_websdr(self) -> None:
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
                await self._webview_host.destroy_page(self._page)
            except Exception as e:
                logger.warning("Non-fatal error destroying WebView: %s", e)
            self._page = None
        was_active = self._websdr_active
        self.site = None
        self._websdr_active = False
        if was_active:
            logger.info("WebSDR session stopped")
        self._publish()

    async def _attach_supervisor(self, page: Page) -> None:
        failures = 0
        while not self.stop_event.is_set() and self._websdr_active:
            if self._driver is None:
                return  # subsystem was stopped out from under this task
            if not self._driver.attached:
                try:
                    await self._driver.attach(page)
                except asyncio.CancelledError:
                    raise
                except (WebSDRIncompatibleError, PlaywrightError) as e:
                    failures += 1
                    delay = min(
                        ATTACH_RETRY_BASE_DELAY_S * (2 ** (failures - 1)),
                        ATTACH_RETRY_MAX_DELAY_S,
                    )
                    logger.warning("WebSDR attach attempt %d failed: %s -- retrying in %.1fs", failures, e, delay)
                    await self._sleep_or_stop(delay)
                    continue
                except Exception as e:
                    # Any other driver bug must not silently kill this task:
                    # that would leave the poll loop running forever with a
                    # permanently-unattached driver and nothing retrying.
                    # Treat it the same as a known attach failure.
                    failures += 1
                    delay = min(
                        ATTACH_RETRY_BASE_DELAY_S * (2 ** (failures - 1)),
                        ATTACH_RETRY_MAX_DELAY_S,
                    )
                    logger.exception(
                        "WebSDR attach attempt %d raised an unexpected error -- retrying in %.1fs", failures, delay
                    )
                    await self._sleep_or_stop(delay)
                    continue
                else:
                    if failures:
                        logger.info("WebSDR attach succeeded after %d failed attempt(s)", failures)
                    failures = 0
                    # The engine may have "sent" freq/mode/ptt during the
                    # outage -- those were no-ops, so the debounce/dedupe
                    # latches must be cleared or the engine will wrongly
                    # believe the WebSDR is already up to date and go silent
                    # until the rig's value physically changes again.
                    self._reset_sync_latches()
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

    # ------------------------------------------------------------------
    def _reset_sync_latches(self) -> None:
        self._last_sent_freq = None
        self._pending_freq = None
        self._last_sent_mode_key = None
        self._last_ptt = None
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
        self._last_reverse_mode_reject_key = None
        self._last_reverse_freq_reject_key = None
        self._reverse_sync_error = None
        self._sync_latch_generation += 1

    async def _poll_loop(self) -> None:
        logger.info("Sync engine started (rig -> WebSDR, one-way)")
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

        if obs_mode is None:
            # Unmapped WebSDR mode (e.g. KiwiSDR's sal/sau/sas/qam/drm/iq,
            # or an OpenWebRX digital mode) -- rate-limited like the
            # rejection case below, so a user parked on an unmapped mode
            # doesn't spam the log every tick but does get one warning.
            if websdr_status.mode != self._last_reverse_mode_reject_key:
                self._last_reverse_mode_reject_key = websdr_status.mode
                logger.debug(
                    "WebSDR mode %r has no reverse hamlib mapping; frequency reverse-sync continues",
                    websdr_status.mode,
                )
        elif obs_mode != self._last_pushed_to_websdr_mode and obs_mode != self._last_observed_mode_key:
            self._last_observed_mode_key = obs_mode
            # Preserve the rig's current filter width when only the
            # page's narrow/wide variant changed within the same base
            # mode (the page can't tell us which variant it wants --
            # WebSDRStatus.mode is already base-collapsed) -- 0 (rig
            # default) only when the base mode itself actually changed.
            passband_hz = state.passband_hz if obs_mode == state.mode else None
            ok = await self._rig.set_mode(obs_mode, passband_hz)
            if generation != self._sync_latch_generation:
                return  # superseded by a concurrent reset while awaiting
            if ok:
                # Also updated here (not just via the reseed path) so a
                # repeat of this same now-applied value on a later tick
                # isn't mistaken for a fresh user edit and re-pushed.
                self._last_pushed_to_websdr_mode = obs_mode
                self._forward_push_completed_at = time.monotonic()
                self._reverse_sync_error = None
                self._last_reverse_mode_reject_key = None
            else:
                # Latch the rejected value as "already considered" even
                # on failure -- otherwise an unchanged page value would
                # re-pass every gate and re-fire set_mode() every single
                # tick forever (a real per-tick hammering loop, worse for
                # flrig where each attempt costs its own poll-until-match
                # budget). A later, genuinely different page value still
                # differs from this and is free to try again.
                self._last_observed_mode_key = obs_mode
                if obs_mode != self._last_reverse_mode_reject_key:
                    self._last_reverse_mode_reject_key = obs_mode
                    self._reverse_sync_error = f"Rig rejected mode {obs_mode!r} pushed from WebSDR"
                    logger.warning(self._reverse_sync_error)
                else:
                    logger.debug("Rig still rejecting mode %r from WebSDR", obs_mode)
            return  # per-tick budget cap: mode takes priority over frequency this tick
        else:
            self._last_observed_mode_key = obs_mode

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
            return

        # Latch as "considered" before the await, regardless of outcome
        # (mirrors the mode branch's rejection handling above) -- an
        # unchanged page value must not re-trigger set_freq() every tick
        # forever after a genuine rejection.
        self._last_observed_freq = self._pending_reverse_freq
        pending_freq = self._pending_reverse_freq
        ok = await self._rig.set_freq(pending_freq)
        if generation != self._sync_latch_generation:
            return  # superseded by a concurrent reset while awaiting
        if ok:
            self._last_pushed_to_websdr_freq = pending_freq
            self._forward_push_completed_at = time.monotonic()
            self._reverse_sync_error = None
            self._last_reverse_freq_reject_key = None
        elif pending_freq != self._last_reverse_freq_reject_key:
            self._last_reverse_freq_reject_key = pending_freq
            self._reverse_sync_error = f"Rig rejected frequency {pending_freq} Hz from WebSDR"
            logger.warning(self._reverse_sync_error)
        else:
            logger.debug("Rig still rejecting frequency %d Hz from WebSDR", pending_freq)

    async def _tick(self) -> None:
        """Runs one poll iteration. A no-op (beyond publishing a status
        snapshot) for whichever subsystem isn't active -- rig and WebSDR
        being independently startable means neither can assume the other
        is present."""
        websdr_status = await self._driver.get_status() if self._websdr_active and self._driver else None

        if not self._rig_active or self._rig is None:
            self._publish(rig_connected=False, websdr=websdr_status)
            return

        if not await self._rig.ensure_connected():
            if self._rig_connect_deadline is not None and time.monotonic() > self._rig_connect_deadline:
                give_up_message = (
                    f"Could not connect to {self._rig_backend} at {self._rig_host}:{self._rig_port} "
                    f"within {RIG_CONNECT_TIMEOUT_S:.0f}s, giving up"
                )
                logger.warning(give_up_message)
                # _stop_rig() publishes its own fresh snapshot (reflecting
                # rig_active=False and, via its cascade, websdr_active=False
                # too) -- don't also publish the stale websdr_status
                # computed above, which describes a session that's about
                # to be torn down.
                await self._stop_rig(error=give_up_message)
                return
            self._publish(rig_connected=False, websdr=websdr_status)
            return

        self._rig_connect_deadline = None
        state = await self._rig.get_state()

        if self._websdr_active and self._driver is not None:
            # Captured before any awaits below, for the reverse gate's
            # own PTT check further down -- see its comment for why.
            last_ptt_before_awaits = self._last_ptt

            if state.ptt is not None and state.ptt != self._last_ptt:
                self._last_ptt = state.ptt
                if state.ptt:
                    if self.settings.mute_on_tx:
                        await self._driver.set_muted(True)
                else:
                    # Always unmute on the falling edge, regardless of the
                    # *current* mute_on_tx value -- if the user unchecks
                    # mute_on_tx while still muted from an earlier TX, the
                    # old gate-both-directions logic would skip this call
                    # (since it also checked mute_on_tx here), leaving the
                    # WebSDR muted indefinitely with no further edge ever
                    # able to clear it. Unmuting when not actually muted
                    # is a harmless no-op.
                    await self._driver.set_muted(False)

            transmitting = bool(self._last_ptt)

            if not transmitting:
                mode_key = (state.mode, state.passband_hz) if state.mode is not None else None
                if mode_key is not None and mode_key != self._last_sent_mode_key:
                    # Only latch as "sent" if the driver actually applied it
                    # -- it may have been a no-op (not attached, e.g.
                    # mid-outage) or a failed page call, and either would
                    # otherwise be wrongly recorded as delivered, silencing
                    # all further retries even after the WebSDR recovers.
                    if await self._driver.set_mode(state.mode, state.passband_hz):
                        self._last_sent_mode_key = mode_key
                        # A mode change can change the effective frequency
                        # sent to the WebSDR (e.g. CW offset), so force a
                        # re-push even if the raw rig frequency itself
                        # didn't move.
                        self._last_sent_freq = None
                        self._last_pushed_to_websdr_mode = state.mode
                        self._forward_push_completed_at = time.monotonic()
                        self._reverse_reseed_due = True

                if state.freq_hz is not None:
                    now = time.monotonic()
                    if self._pending_freq is None or abs(state.freq_hz - self._pending_freq) > FREQ_CHANGE_THRESHOLD_HZ:
                        self._pending_freq = state.freq_hz
                        self._pending_freq_since = now
                    elif (
                        now - self._pending_freq_since >= FREQ_DEBOUNCE_S
                        and (self._last_sent_freq is None
                             or abs(self._pending_freq - self._last_sent_freq) > FREQ_CHANGE_THRESHOLD_HZ)
                    ):
                        if await self._driver.tune_hz(self._pending_freq):
                            self._last_sent_freq = self._pending_freq
                            self._last_pushed_to_websdr_freq = self._pending_freq
                            self._forward_push_completed_at = time.monotonic()
                            self._reverse_reseed_due = True

            websdr_status = await self._driver.get_status()

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
                not reverse_ptt
                and websdr_status.connected
                and time.monotonic() - self._forward_push_completed_at >= REVERSE_HOLDOFF_S
            ):
                await self._reverse_sync_tick(websdr_status, state)

        self._publish(
            rig_connected=True,
            rig_freq_hz=state.freq_hz,
            rig_mode=state.mode,
            rig_ptt=state.ptt,
            websdr=websdr_status,
        )


def _describe_webview_create_error(e: Exception) -> str:
    return f"Could not create the WebSDR browser view: {e}"
