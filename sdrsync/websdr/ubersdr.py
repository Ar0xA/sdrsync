"""Driver for the UberSDR software family (https://github.com/madpsy/ka9q_ubersdr).

Strategy: unlike the other three drivers, this one does NOT reach into the
page's internal globals. UberSDR's v2 interface publishes a documented,
versioned control API for exactly this purpose -- the same one its own
Chrome and Firefox extensions use -- so this driver is a client of that API
and nothing else. The contract is `static/v2/BRIDGE_API.md` in the UberSDR
source tree; its tests there are its specification.

That difference is worth stating plainly, because it changes what can break:

    websdr.org/KiwiSDR/OpenWebRX drivers depend on undocumented page
    internals (`ext_tune`, `demodulators[0]`, `window.ws`). They work, and
    WebSDRIncompatibleError exists because those can change without notice.

    Here, the page promises a stable protocol with a version number and a
    capability list, refuses bad input with a *reason* instead of clamping,
    and answers every message -- including "the operator switched the bridge
    off", which is a specific, reportable state rather than a silence.

── The API, in the shape this driver uses it ──────────────────────────────

Transport: two CustomEvents on `window`, each carrying one JSON string.

    ubersdr.to-page      client -> page
    ubersdr.from-page    page -> client

Every client message carries `{v:1, from:'client', client:<our id>, id:<n>,
type:...}` and gets exactly one reply. `hello` is answered with `announce`
(the receiver's identity, capabilities and topic list). `subscribe` returns a
full snapshot of each topic and then pushes only what changed. `command`
returns the state it just set, so no follow-up read is needed.

The commands used here, and why each:

    tune {frequency, ensureVisible}          the dial
    tune {mode, bandwidthLow, bandwidthHigh} mode and filter in ONE call
    duck {ducked}                            transient silence -- see set_muted
    bye                                      release our client slot

`tune` carries mode and passband together deliberately: sent separately, the
receiver passes through the new mode's *default* filter on the way to the one
we wanted, and that is audible. The API says so, and it is the reason
set_mode() sends `tune` rather than the `mode` command.

── Why an injected agent, and why one round trip ─────────────────────────

The transport is event-driven and the browser shim's evaluate() is
request/response over JSON, so a tiny agent is installed in the page (see
_AGENT_JS) to hold the client id, the merged topic state and the replies.
Everything below then reads or drives that agent.

The page's host handles a message *synchronously* inside our dispatchEvent
call -- verified by reading its implementation (`src/bridge/host.js`:
`handle` -> `settle` -> `send`, with no await on the synchronous command
path). So the ordinary case is one evaluate: dispatch, and the reply is
already in hand when dispatchEvent returns. `_call` still polls, because a
command *may* answer asynchronously and the protocol permits it -- but the
poll is the exception, not the normal cost.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from sdrsync.websdr.browser_shim import BrowserError as PlaywrightError
from sdrsync.websdr.browser_shim import PageLike as Page

from sdrsync.websdr.base import WebSDRIncompatibleError, WebSDRStatus, same_site

logger = logging.getLogger("sdrsync.websdr.ubersdr")

LOAD_TIMEOUT_MS = 20000
# How long a command's reply is waited for, and how often it is checked. Only
# reached for a command the page answers asynchronously; the synchronous ones
# are in hand before the first check.
CALL_TIMEOUT_S = 3.0
CALL_POLL_S = 0.05
# Audio needs a press: the receiver deliberately refuses `power {on:true}`
# because browsers require a user gesture to start playback. Two attempts are
# made after the page is otherwise ready, then it is left alone -- see
# _start_audio for why a failure there must not fail the attach.
AUDIO_START_ATTEMPTS = 2
AUDIO_START_WAIT_S = 1.5
# How long a reattach waits for the page already loaded to answer before deciding
# it needs reloading. Short: a page that is still the right page answers in the
# time it takes to dispatch an event, and the only cost of being wrong here is one
# navigation that was going to happen anyway.
RETRY_HANDSHAKE_MS = 2000

# The protocol version this driver speaks, and the API major it was written
# against. Checked, not assumed: the API's own rule is "check v and api.major,
# feature-detect everything else on capabilities, never branch on api.minor".
PROTOCOL_V = 1
API_MAJOR = 1

# hamlib mode name -> UberSDR mode id. From the receiver's own mode table
# (`static/v2/src/radio/constants.js`): lsb, usb, am, sam, nfm, fm, cwl, cwu.
#
# CW is the one that needs care. UberSDR has cwu and cwl, and the letter says
# which sideband the *tone* is on -- the filter is symmetric about the dial
# either way. hamlib's CW/CWR carry the same distinction, so they map straight
# across rather than both collapsing onto one.
_MODE_MAP: dict[str, str] = {
    "USB": "usb",
    "PKTUSB": "usb",
    "DATA-U": "usb",
    "LSB": "lsb",
    "PKTLSB": "lsb",
    "DATA-L": "lsb",
    "CW": "cwu",
    "CW-U": "cwu",
    "CWR": "cwl",
    "CW-L": "cwl",
    "AM": "am",
    "AMS": "sam",
    "SAM": "sam",
    "FM": "nfm",
    "FMN": "nfm",
    "WFM": "fm",
    "FM-WIDE": "fm",
}

# Fallback passband edges and limits, used only if the page's own `modes` topic
# could not be read. The live table is preferred (see _load_modes) so a
# receiver whose limits differ from this build is still driven correctly.
_FALLBACK_MODES: dict[str, dict[str, Any]] = {
    "usb": {"low": 50, "high": 2700, "min": 0, "max": 6000, "sideband": "upper"},
    "lsb": {"low": -2700, "high": -50, "min": -6000, "max": 0, "sideband": "lower"},
    "am": {"low": -5000, "high": 5000, "min": -8000, "max": 8000, "sideband": "both"},
    "sam": {"low": -5000, "high": 5000, "min": -8000, "max": 8000, "sideband": "both"},
    "nfm": {"low": -5000, "high": 5000, "min": -8000, "max": 8000, "sideband": "both"},
    "fm": {"low": -8000, "high": 8000, "min": -12000, "max": 12000, "sideband": "both"},
    "cwu": {"low": -200, "high": 200, "min": -500, "max": 500, "sideband": "both"},
    "cwl": {"low": -200, "high": 200, "min": -500, "max": 500, "sideband": "both"},
}


def map_hamlib_mode_ubersdr(hamlib_mode: str) -> Optional[str]:
    """Pure mapping from a hamlib mode name to an UberSDR mode id.

    Returns None when there is no equivalent (the caller logs and skips mode
    sync, leaving frequency sync unaffected -- the same contract as the other
    drivers). No passband argument, unlike KiwiSDR's mapper: UberSDR takes the
    filter as real numbers, so the width does not have to be smuggled into the
    choice of mode string. See passband_edges().
    """
    return _MODE_MAP.get(hamlib_mode.upper())


# Reverse of _MODE_MAP's collapsing cases, for reverse sync (WebSDR -> rig,
# v11). get_status() reports the receiver's mode id uppercased (see
# get_status() below), so the keys here are upper too. One canonical hamlib
# name per UberSDR id, picked the same way KiwiSDR's _REVERSE_MODE_MAP picks
# one: nfm collapses FM/FMN's many hamlib spellings back to the plain one,
# and cwu/cwl keep CW/CWR distinct because the API (and _is_cw() below) does.
_REVERSE_MODE_MAP: dict[str, str] = {
    "USB": "USB",
    "LSB": "LSB",
    "AM": "AM",
    "SAM": "SAM",
    "NFM": "FM",
    "FM": "WFM",
    "CWU": "CW",
    "CWL": "CWR",
}


def map_ubersdr_mode_to_hamlib(mode: Optional[str]) -> Optional[str]:
    """Pure reverse mapping: get_status()-normalized mode string (already
    uppercased) -> a canonical hamlib mode name. None if unknown (caller
    should skip the reverse push and log, not raise)."""
    if mode is None:
        return None
    return _REVERSE_MODE_MAP.get(mode.upper())


def passband_edges(
    mode_id: str, width_hz: Optional[int], modes: Optional[dict[str, dict[str, Any]]] = None
) -> Optional[tuple[int, int]]:
    """The filter edges to ask for, in Hz relative to the dial, or None.

    None means "say nothing about the filter", which leaves the receiver on the
    mode's own default -- the right answer when the rig did not report a
    passband, or reported something the mode cannot do.

    UberSDR's filter is two edges relative to the tuned frequency, and which
    edges a width becomes depends on the mode's sideband:

        upper (usb)  the low edge stays where the mode puts it (50 Hz, so the
                     filter does not open onto the carrier) and the width is
                     added above it.
        lower (lsb)  the mirror of that.
        both (cw, am, sam, fm)  symmetric about the dial: a 500 Hz rig filter
                     is -250..+250, because that is what a symmetric filter of
                     that width *is*. Halving it here rather than passing the
                     width twice is the difference between matching the rig and
                     being twice as wide as it.

    Edges are clamped into the mode's limits rather than sent and refused: a rig
    with a 3.5 kHz SSB filter on a receiver that allows 6 kHz is fine, and one
    with a 15 kHz FM filter on a receiver that allows 12 is asking for the
    widest the receiver has, which is the honest reading of "as wide as I am".
    """
    table = modes or _FALLBACK_MODES
    info = table.get(mode_id)
    if info is None or not width_hz or width_hz <= 0:
        return None

    lo_limit = int(info["min"])
    hi_limit = int(info["max"])
    sideband = info.get("sideband") or "both"

    if sideband == "upper":
        low = int(info["low"])
        high = low + int(width_hz)
    elif sideband == "lower":
        high = int(info["high"])
        low = high - int(width_hz)
    else:
        half = int(round(width_hz / 2))
        low, high = -half, half

    low = max(lo_limit, min(hi_limit, low))
    high = max(lo_limit, min(hi_limit, high))
    # A clamp can collapse the two edges onto each other (a width of 1 Hz, or a
    # limit table that disagrees with the defaults). A filter of no width is not
    # a filter, so fall back to the mode's own rather than sending nonsense the
    # receiver would refuse.
    if high - low < 10:
        return None
    return low, high


def v2_page_url(url: str) -> str:
    """The v2 interface's URL for a site URL somebody pasted.

    Only the v2 interface has the control API this driver speaks -- the older
    one has no such thing -- and an operator pasting their receiver's address
    will paste the root. So `https://rx.example/` becomes
    `https://rx.example/v2/`, while a URL already pointing at v2 is left alone.

    The query string is preserved, because UberSDR's own share links carry the
    frequency and mode in it and pasting one should land where it says.
    """
    parts = urlsplit(url if "//" in url else f"http://{url}")
    path = parts.path or "/"
    segments = [s for s in path.split("/") if s]
    if segments and segments[-1] == "v2":
        return urlunsplit(parts._replace(path=path if path.endswith("/") else path + "/"))
    if "v2" in segments:
        # Already inside the v2 interface somewhere (a sub-path, or a URL with a
        # file on the end). Left exactly as given.
        return urlunsplit(parts)
    return urlunsplit(parts._replace(path=path.rstrip("/") + "/v2/"))


# The in-page agent. Installed once per page load, idempotent, and deliberately
# small: it is a transport, not logic. Everything it knows is either the
# protocol's own vocabulary or something the page told it.
#
# Written as a function source returning a JSON-able summary, because that is what
# the browser shim's evaluate() calls: it builds `JSON.stringify((js)(args))`, so an
# IIFE here would be invoked twice -- once by itself and once by the shim, the
# second time on its own return value.
_AGENT_JS = r"""
() => {
    var KEY = '__sdrsyncUberSDR';
    if (window[KEY] && window[KEY].v === 1) {
        return {installed: true, ready: window[KEY].ready, refused: window[KEY].refused};
    }
    var agent = {
        v: 1,
        client: 'sdrsync-' + Math.random().toString(36).slice(2, 10),
        nextId: 1,
        ready: false,          // an announce has arrived
        refused: null,         // the page answered, but said no (e.g. 'disabled')
        descriptor: null,
        topics: {},            // merged from snapshots and patches
        results: {},           // id -> {ok, value, error}
        closed: false,
    };
    window[KEY] = agent;

    window.addEventListener('ubersdr.from-page', function (ev) {
        var msg;
        try { msg = JSON.parse(ev.detail); } catch (e) { return; }
        if (!msg || msg.v !== 1 || msg.from !== 'page') return;
        // Addressed messages that are not ours are somebody else's client on the
        // same page -- an extension, a userscript. Left alone.
        if (msg.client && msg.client !== agent.client) return;

        if (msg.type === 'announce') {
            // Every announce means "reset and re-subscribe": the page has
            // (re)started and anything we knew about its state is from a previous
            // life. Re-subscribing is not optional -- the page sends *patches*
            // against what it last sent us, so after a restart a topic that has
            // not changed since would never be sent again and our picture would
            // stay empty. The browser extensions do exactly this on announce.
            agent.descriptor = msg;
            agent.topics = {};
            agent.ready = true;
            agent.refused = null;
            agent.closed = false;
            agent.resubscribe();
            return;
        }
        if (msg.type === 'state') {
            var cur = agent.topics[msg.topic] || {};
            var patch = msg.patch || {};
            for (var k in patch) cur[k] = patch[k];
            agent.topics[msg.topic] = cur;
            return;
        }
        if (msg.type === 'result') {
            // Our own re-subscribe, which nobody is waiting on: its value is a
            // full snapshot of each topic, so it is merged rather than queued.
            if (agent.seedIds[msg.id]) {
                delete agent.seedIds[msg.id];
                if (msg.ok) agent.seed(msg.value);
                return;
            }
            agent.results[msg.id] = {ok: !!msg.ok, value: msg.value, error: msg.error || null};
            if (!msg.ok && msg.error && msg.error.code === 'disabled') {
                agent.refused = msg.error.message || 'the browser bridge is switched off';
            }
            return;
        }
        if (msg.type === 'closing') {
            agent.closed = true;
            agent.ready = false;
        }
    });

    // Defined after the listener that calls them, deliberately: the listener only
    // runs on an event, which cannot happen until this function has returned and
    // the agent is whole.
    // The id is chosen by the caller, because the page answers *inside* this
    // dispatch: anything that has to be true before the reply arrives has to be
    // true before the dispatch, not after it returns. See resubscribe(), where
    // getting that wrong made the reply arrive before it could be recognised.
    agent.emit = function (id, type, fields) {
        var msg = {v: 1, from: 'client', client: agent.client, id: id, type: type};
        for (var k in (fields || {})) msg[k] = fields[k];
        window.dispatchEvent(new CustomEvent('ubersdr.to-page', {detail: JSON.stringify(msg)}));
        return id;
    };

    agent.send = function (type, fields) {
        return agent.emit(agent.nextId++, type, fields);
    };

    // Send and collect in one go. The page answers a synchronous command inside
    // our dispatch, so the reply is usually already here; when it is not, the
    // caller polls take().
    agent.call = function (type, fields) {
        var id = agent.send(type, fields);
        var res = agent.results[id];
        if (res) { delete agent.results[id]; return {id: id, done: true, res: res}; }
        return {id: id, done: false, res: null};
    };

    // What to ask for again after an announce. Set once by the driver; re-sent by
    // the agent, because an announce can arrive at any moment and the driver is
    // not watching the channel.
    agent.subscription = null;
    agent.seedIds = {};
    agent.resubscribe = function () {
        if (!agent.subscription) return;
        // Marked as ours before it goes out. The page replies synchronously, so
        // `seedIds[send(...)] = true` would assign after the reply had already been
        // handled -- and the reply, unrecognised, would be queued as though somebody
        // were waiting for it while the topics stayed empty.
        var id = agent.nextId++;
        agent.seedIds[id] = true;
        agent.emit(id, 'subscribe', agent.subscription);
    };

    agent.take = function (id) {
        var res = agent.results[id];
        if (!res) return null;
        delete agent.results[id];
        return res;
    };

    // Merge snapshots from a subscribe reply, which carries a full one per topic.
    agent.seed = function (value) {
        for (var t in (value || {})) agent.topics[t] = value[t];
    };

    return {installed: true, ready: agent.ready, refused: agent.refused};
}
"""

_KEY = "window.__sdrsyncUberSDR"


class UberSDRDriver:
    """WebSDRDriver implementation for the UberSDR software family.

    Does not own the page's lifecycle -- see WebsdrOrgDriver's docstring, same
    contract: the engine creates and destroys the WebView, this drives it.
    """

    # v2's own bundle, and v1's extension detector. Both are UberSDR-specific,
    # and having both means a pasted root URL is recognised as well as a pasted
    # /v2/ one -- the root serves the older interface, which this driver then
    # navigates away from (see v2_page_url).
    FINGERPRINT_MARKERS = ("dist/v2.js", "browser-extension-detector.js")

    def __init__(self, url: str, cw_offset_hz: int = 0) -> None:
        self.url = v2_page_url(url)
        self.cw_offset_hz = cw_offset_hz

        self._page: Optional[Page] = None
        self._attached = False
        self._listeners_registered = False
        self._current_mode: Optional[str] = None
        self._modes: Optional[dict[str, dict[str, Any]]] = None
        self._receiver: dict[str, Any] = {}
        self._capabilities: tuple[str, ...] = ()
        # Whether this receiver has the transient-silence command. Read from the
        # announce, not assumed -- see set_muted.
        self._can_duck = True
        self._last_attach_error: Optional[str] = None
        self._last_page_error: Optional[str] = None
        self._last_tune_error: Optional[str] = None
        self._last_mode_error: Optional[str] = None
        # Rate-limits the "no equivalent" warning to once per new mode, as the
        # other drivers do: the engine retries set_mode() every poll tick.
        self._last_unmapped_mode: Optional[str] = None
        self._audio_started = False

    @property
    def attached(self) -> bool:
        return self._attached

    # ------------------------------------------------------------------ attach
    async def attach(self, page: Page) -> None:
        self._page = page
        if not self._listeners_registered:
            page.on("console", self._on_console)
            page.on("pageerror", self._on_pageerror)
            self._listeners_registered = True

        # A reattach on a page that is still the right page should not reload it.
        # Reloading costs the operator's audio session and everything the page had
        # set up, and a reattach is not always the page's fault: we can be let go
        # to make room (the page keeps at most eight clients and evicts the
        # stalest), or a single script call can have timed out. So: say hello to
        # what is already there, and only navigate if that gets no answer.
        #
        # But the handshake alone can't tell "the right page, just reachable
        # again" apart from "a DIFFERENT UberSDR site that happens to still be
        # loaded and already speaks v2" (e.g. engine.py's _switch_websdr()
        # reusing the same page to switch between two UberSDR stations) --
        # the previous site's agent is still installed and ready, so it would
        # answer hello too. Gate on same_site() first so a genuine switch
        # always navigates instead of silently continuing to control the old
        # receiver.
        current_url = await page.evaluate("() => window.location.href")
        if not (same_site(current_url, self.url) and await self._handshake(RETRY_HANDSHAKE_MS)):
            try:
                await page.goto(self.url, timeout=LOAD_TIMEOUT_MS)
            except PlaywrightError as e:
                self._last_attach_error = f"Could not load {self.url}: {e!r}"
                raise WebSDRIncompatibleError(self._last_attach_error) from e
            if not await self._handshake(LOAD_TIMEOUT_MS):
                self._last_attach_error = (
                    f"Page at {self.url} did not answer the UberSDR v2 control API within "
                    f"{LOAD_TIMEOUT_MS}ms. Is this an UberSDR running the v2 interface?"
                )
                raise WebSDRIncompatibleError(self._last_attach_error)

        refused = await self._agent_field("refused")
        if refused:
            # A specific, actionable state rather than a mystery: the operator
            # has a switch for this API and it is off.
            self._last_attach_error = (
                f"UberSDR at {self.url} refused control: {refused}. Switch it on in the "
                f"receiver's SDR Control panel (Browser bridge)."
            )
            raise WebSDRIncompatibleError(self._last_attach_error)

        await self._read_descriptor()
        await self._subscribe()
        await self._load_modes()

        self._attached = True
        self._current_mode = None
        self._audio_started = False
        self._last_attach_error = None
        self._last_page_error = None
        self._last_tune_error = None
        self._last_mode_error = None
        self._last_unmapped_mode = None

        name = self._receiver.get("name") or self._receiver.get("callsign") or "UberSDR"
        logger.info(
            "Attached to %s (%s) via the v2 control API; capabilities: %s",
            name, self.url, ", ".join(self._capabilities) or "none reported",
        )

        # Last, and never fatal: the receiver is controllable without audio, and
        # a rig that retunes a silent WebSDR is still doing its job.
        await self._start_audio()

    async def _handshake(self, timeout_ms: int) -> bool:
        """Install the agent, say hello, and wait for the page to answer.

        This is the identification, and it is the same one the browser extensions
        do: a page that answers with an `announce` *is* an UberSDR with a working
        control API, and one that does not is not, whatever its HTML serves. A
        script-tag fingerprint can only guess at that; this settles it.

        `refused` counts as an answer -- the bridge being switched off is the page
        talking to us, and reloading would not change its mind.
        """
        try:
            await self._install_agent()
            await self._page.wait_for_function(
                f"!!({_KEY} && ({_KEY}.ready || {_KEY}.refused))", timeout=timeout_ms,
            )
            return True
        except PlaywrightError:
            return False

    async def _install_agent(self) -> None:
        await self._page.evaluate(_AGENT_JS)
        # `hello` is answered with an announce addressed to us. Sent every
        # attach, including a reattach on an already-loaded page: the page may
        # have announced before the agent existed.
        await self._page.evaluate(f"() => {{ {_KEY}.send('hello'); return true; }}")

    async def _agent_field(self, name: str) -> Any:
        try:
            return await self._page.evaluate(f"() => ({_KEY} ? {_KEY}.{name} : null)")
        except PlaywrightError:
            return None

    async def _read_descriptor(self) -> None:
        d = await self._agent_field("descriptor") or {}
        api = d.get("api") or {}
        # The protocol's own compatibility rule: check the envelope version and
        # the API major, feature-detect the rest on capabilities. A major bump
        # means the meanings changed, which is the one thing this cannot adapt
        # to on its own.
        if d.get("v") not in (None, PROTOCOL_V) or api.get("major") not in (None, API_MAJOR):
            self._last_attach_error = (
                f"UberSDR at {self.url} speaks protocol v{d.get('v')} API "
                f"{api.get('major')}.{api.get('minor')}; this driver speaks v{PROTOCOL_V} "
                f"API {API_MAJOR}.x. Update sdrsync."
            )
            raise WebSDRIncompatibleError(self._last_attach_error)
        self._receiver = d.get("receiver") or {}
        caps = d.get("capabilities") or []
        self._capabilities = tuple(str(c) for c in caps)
        # An empty list means an announce we could not read, not a receiver with
        # no commands -- so the good case is assumed and a refusal is reported
        # rather than a silence being introduced by our own guess.
        self._can_duck = ("duck" in self._capabilities) if self._capabilities else True
        if self._capabilities and not self._can_duck:
            logger.warning(
                "UberSDR at %s has no `duck` command; transmit silence will use the "
                "operator's own mute instead", self.url,
            )

    async def _subscribe(self) -> None:
        """Subscribe to what get_status() reports, and seed it with a snapshot.

        Three topics and not `signal`: the meter changes continuously and
        nothing here shows it, so asking for it would be ten messages a second
        into a variable nobody reads.
        """
        topics = ["tuning", "audio", "session"]
        # Remembered in the page, so the agent can re-send it the moment the page
        # announces again -- an announce resets what the page thinks we have been
        # told, and patches after it are diffs against nothing.
        await self._page.evaluate(
            f"(t) => {{ {_KEY}.subscription = {{topics: t}}; return true; }}", topics
        )
        res = await self._call("subscribe", None, topics=topics)
        if res is not None and res.get("ok"):
            await self._page.evaluate(
                f"(v) => {{ {_KEY}.seed(v); return true; }}", res.get("value") or {}
            )

    async def _load_modes(self) -> None:
        """The receiver's own mode table, for turning a rig passband into edges.

        Read from the page rather than hardcoded so a receiver whose limits
        differ from this build's assumptions is still driven correctly. A
        failure here is not fatal: _FALLBACK_MODES covers it, and the worst case
        is a filter the receiver refuses with a reason.
        """
        res = await self._call("get", None, topic="modes")
        rows = (res or {}).get("value") if res and res.get("ok") else None
        if not isinstance(rows, list):
            logger.debug("Could not read the receiver's mode table; using built-in defaults")
            return
        table: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            default = row.get("default") or {}
            limits = row.get("limits") or {}
            table[str(row["id"])] = {
                "low": default.get("low", 0),
                "high": default.get("high", 0),
                "min": limits.get("min", 0),
                "max": limits.get("max", 0),
                "sideband": limits.get("sideband") or "both",
            }
        if table:
            self._modes = table

    async def _start_audio(self) -> None:
        """Press Start, because the receiver will not do it for us.

        `power {on:true}` is refused by design -- browsers require a user
        gesture to begin playback -- so the page shows a Start overlay and waits.
        Two attempts: the button's own click first, and a real pointer click at
        its coordinates second, which is what the other drivers use to satisfy
        an autoplay gate.

        Never fatal. Frequency and mode sync work on a silent receiver, and
        reporting audio_active=False is more useful than refusing to attach.
        """
        for attempt in range(AUDIO_START_ATTEMPTS):
            if await self._running():
                self._audio_started = True
                return
            try:
                box = await self._page.evaluate(
                    "() => { var b = document.querySelector('.start__go');"
                    " if (!b) return null;"
                    " var r = b.getBoundingClientRect();"
                    " return {x: r.left + r.width / 2, y: r.top + r.height / 2}; }"
                )
            except PlaywrightError:
                box = None
            if box is None:
                # No overlay: either it is already running (caught above on the
                # next pass) or this page does not gate audio.
                await asyncio.sleep(AUDIO_START_WAIT_S)
                continue
            try:
                if attempt == 0:
                    await self._page.evaluate(
                        "() => { var b = document.querySelector('.start__go');"
                        " if (b) b.click(); return true; }"
                    )
                else:
                    await self._page.mouse.click(int(box["x"]), int(box["y"]))
            except PlaywrightError as e:
                logger.debug("Could not press the receiver's Start button: %s", e)
            await asyncio.sleep(AUDIO_START_WAIT_S)

        self._audio_started = await self._running()
        if not self._audio_started:
            logger.warning(
                "UberSDR at %s is tuned and controllable but its audio has not started "
                "(the page's Start button did not take). Frequency and mode sync are "
                "unaffected.", self.url,
            )

    async def _running(self) -> bool:
        session = await self._topic("session")
        return bool(session.get("running")) if session else False

    # ------------------------------------------------------------------- tuning
    async def tune_hz(self, freq_hz: int, verify: bool = True) -> bool:
        """Returns True only if the receiver applied it.

        No readback: the command returns the tuning it just set, and an
        impossible frequency comes back as `bad_args` with a reason rather than
        being clamped silently. That is stricter than a readback comparison and
        the reason gets logged.

        verify is accepted for WebSDRDriver Protocol parity but unused here --
        the command's own reply settles success/failure synchronously, so
        there's no delayed background correction for verify=False to skip
        (same reasoning as KiwiSDR's and OpenWebRX's tune_hz()).
        """
        if not self._attached:
            return False
        effective_hz = freq_hz + (self.cw_offset_hz if self._is_cw() else 0)

        res = await self._call(
            "command", "tune",
            name="tune",
            args={"frequency": int(effective_hz), "ensureVisible": True},
        )
        if res is None:
            return False
        if not res.get("ok"):
            self._last_tune_error = self._error_text("tune", res)
            logger.warning(self._last_tune_error)
            return False
        self._last_tune_error = None
        return True

    async def set_mode(self, hamlib_mode: str, passband_hz: Optional[int]) -> bool:
        """Mode and filter in one command. Returns True only if applied.

        One command and not two: `mode` alone would set the mode's default
        filter and a following `passband` would replace it, so the receiver
        would pass audibly through the wrong width. `tune` takes both together,
        which is what the API documents it for.
        """
        if not self._attached:
            return False
        mode_id = map_hamlib_mode_ubersdr(hamlib_mode)
        if mode_id is None:
            self._last_mode_error = (
                f"hamlib mode {hamlib_mode!r} has no UberSDR equivalent; frequency sync continues"
            )
            if hamlib_mode != self._last_unmapped_mode:
                self._last_unmapped_mode = hamlib_mode
                logger.warning(self._last_mode_error)
            else:
                logger.debug(self._last_mode_error)
            return False

        args: dict[str, Any] = {"mode": mode_id}
        edges = passband_edges(mode_id, passband_hz, self._modes)
        if edges is not None:
            args["bandwidthLow"], args["bandwidthHigh"] = edges

        res = await self._call("command", "tune", name="tune", args=args)
        if res is None:
            return False
        if not res.get("ok"):
            self._last_mode_error = self._error_text("mode", res)
            logger.warning(self._last_mode_error)
            # A refused *filter* must not cost us the mode change: retry with the
            # mode alone, which leaves the receiver on its own default width.
            if edges is not None:
                retry = await self._call("command", "tune", name="tune", args={"mode": mode_id})
                if retry is not None and retry.get("ok"):
                    logger.info(
                        "UberSDR refused the rig's %d Hz filter for %s; used the receiver's "
                        "own passband instead", passband_hz or 0, mode_id,
                    )
                    self._current_mode = mode_id
                    self._last_mode_error = None
                    self._last_unmapped_mode = None
                    return True
            return False

        self._current_mode = mode_id
        self._last_mode_error = None
        self._last_unmapped_mode = None
        return True

    async def set_muted(self, muted: bool) -> None:
        """Silence for transmit -- with `duck` where there is one, and that is the point.

        UberSDR draws a distinction the other families do not: `mute` is the
        operator's own setting, which the page's mute button shows and that
        browser remembers between visits, while `duck` is transient silence
        applied by something else. Transmit is the second kind. Using `duck`
        means sdrsync dying mid-transmission leaves nothing behind to undo, and
        the mute button in the view never shows a state nobody chose.

        Feature-detected on the announce's capability list rather than assumed,
        which is the API's own rule -- and the fallback matters: a receiver
        without `duck` still has to go quiet while the rig is transmitting, so
        `mute` is used instead and said out loud once. RX audio over your own
        transmission is the worse failure.

        Absolute, never a toggle: PTT arrives as "transmitting: true/false", and
        a toggle desynchronises permanently the first time a message is missed.
        """
        if not self._attached:
            return
        if self._can_duck:
            name, args = "duck", {"ducked": bool(muted)}
        else:
            name, args = "mute", {"muted": bool(muted)}
        res = await self._call("command", name, name=name, args=args)
        if res is not None and not res.get("ok"):
            logger.debug("%s command refused: %s", name, self._error_text(name, res))

    # ------------------------------------------------------------------- status
    async def get_status(self) -> WebSDRStatus:
        if not self._attached:
            return WebSDRStatus(connected=False, last_error=self._combined_error())
        try:
            state = await self._page.evaluate(
                f"() => ({_KEY} ? {{ready: {_KEY}.ready, closed: {_KEY}.closed,"
                f" refused: {_KEY}.refused, topics: {_KEY}.topics}} : null)"
            )
        except PlaywrightError as e:
            self._attached = False
            self._last_page_error = str(e)
            return WebSDRStatus(connected=False, last_error=self._combined_error())

        if not state:
            # The agent is gone, which means the page reloaded under us. Drop
            # attachment so the engine's supervisor re-attaches rather than
            # driving a page that is no longer listening.
            self._attached = False
            self._last_page_error = "The UberSDR page reloaded; re-attaching"
            return WebSDRStatus(connected=False, last_error=self._combined_error())
        if state.get("closed") or not state.get("ready"):
            self._attached = False
            self._last_page_error = (
                state.get("refused")
                or "The UberSDR page closed the control connection; re-attaching"
            )
            return WebSDRStatus(connected=False, last_error=self._combined_error())

        topics = state.get("topics") or {}
        tuning = topics.get("tuning") or {}
        session = topics.get("session") or {}
        audio = topics.get("audio") or {}
        freq = tuning.get("frequency")
        mode = tuning.get("mode")
        return WebSDRStatus(
            connected=True,
            freq_hz=int(freq) if isinstance(freq, (int, float)) else None,
            mode=str(mode).upper() if mode else None,
            # The receiver's own answer to "is audio playing", pushed to us
            # rather than inferred from a socket's readyState as the other
            # drivers must. `ducked` is folded in because a ducked receiver is
            # silent, and reporting it as playing would be a lie a user can hear.
            audio_active=bool(session.get("running")) and not bool(audio.get("ducked")),
            last_error=self._combined_error(),
        )

    async def close(self) -> None:
        # Say goodbye, so the slot is freed rather than waiting to be evicted:
        # the page holds at most eight clients, and a development session that
        # reconnects repeatedly would otherwise push out somebody's extension.
        if self._page is not None and self._attached:
            try:
                await self._page.evaluate(f"() => {{ {_KEY} && {_KEY}.send('bye'); return true; }}")
            except PlaywrightError:
                pass
        self._attached = False
        self._page = None

    def _reverse_effective_hz(
        self, observed_hz: Optional[int], observed_hamlib_mode: Optional[str]
    ) -> Optional[int]:
        """Un-applies cw_offset_hz for the reverse direction (WebSDR ->
        rig), symmetric to tune_hz()'s forward application above. Takes
        the mode as an explicit argument, sourced from the SAME status
        snapshot map_ubersdr_mode_to_hamlib() derived it from -- NOT
        self._current_mode, which only reflects this driver's own last
        PUSHED mode and is stale by construction for reverse sync (a page
        change the driver didn't itself push, e.g. someone else on the
        receiver retuning it, is exactly what reverse sync exists to
        observe)."""
        if observed_hz is None:
            return None
        if observed_hamlib_mode in ("CW", "CWR"):
            return observed_hz - self.cw_offset_hz
        return observed_hz

    def hamlib_mode_from_status(self, status: WebSDRStatus) -> Optional[str]:
        return map_ubersdr_mode_to_hamlib(status.mode)

    def rig_freq_from_status(self, status: WebSDRStatus) -> Optional[int]:
        return self._reverse_effective_hz(status.freq_hz, self.hamlib_mode_from_status(status))

    # ---------------------------------------------------------------- internals
    def _is_cw(self) -> bool:
        return self._current_mode in ("cwu", "cwl")

    async def _topic(self, name: str) -> dict[str, Any]:
        try:
            got = await self._page.evaluate(
                f"() => ({_KEY} && {_KEY}.topics ? ({_KEY}.topics['{name}'] || null) : null)"
            )
        except PlaywrightError:
            return {}
        return got if isinstance(got, dict) else {}

    async def _call(self, msg_type: str, label: Optional[str], **fields: Any) -> Optional[dict]:
        """Send one message and return its reply, or None if it never came.

        The page answers a synchronous command inside our own dispatch, so the
        common case is settled by the first evaluate and the loop below never
        runs. It exists because the protocol allows an asynchronous answer, and
        a client that assumed otherwise would work until the day one command
        started returning a promise.
        """
        if self._page is None:
            return None
        try:
            first = await self._page.evaluate(
                f"(f) => {_KEY}.call(f.type, f.fields)",
                {"type": msg_type, "fields": fields},
            )
        except PlaywrightError as e:
            self._note_call_failure(label, f"could not reach the UberSDR page: {e}")
            return None
        if not isinstance(first, dict):
            self._note_call_failure(label, "the in-page control agent is missing")
            return None
        if first.get("done"):
            return first.get("res")

        call_id = first.get("id")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CALL_TIMEOUT_S
        while loop.time() < deadline:
            await asyncio.sleep(CALL_POLL_S)
            try:
                res = await self._page.evaluate(f"(id) => {_KEY}.take(id)", call_id)
            except PlaywrightError as e:
                self._note_call_failure(label, f"could not reach the UberSDR page: {e}")
                return None
            if isinstance(res, dict):
                return res
        self._note_call_failure(label, f"no reply within {CALL_TIMEOUT_S:.0f}s")
        return None

    def _note_call_failure(self, label: Optional[str], text: str) -> None:
        message = f"UberSDR {label or 'control'} call failed: {text}"
        logger.warning(message)
        if label == "tune":
            self._last_tune_error = message
        elif label == "mode":
            self._last_mode_error = message
        else:
            self._last_page_error = message

    @staticmethod
    def _error_text(label: str, res: dict) -> str:
        err = res.get("error") or {}
        code = err.get("code") or "failed"
        message = err.get("message") or "no reason given"
        # The reason is the receiver's own words, which is the whole advantage of
        # a documented API over a readback: "frequency 40000000 is outside
        # 10000-30000000" says what to fix.
        return f"UberSDR refused the {label} ({code}): {message}"

    def _combined_error(self) -> Optional[str]:
        parts = [
            e for e in (
                self._last_attach_error, self._last_page_error,
                self._last_tune_error, self._last_mode_error,
            ) if e
        ]
        return " | ".join(parts) if parts else None

    def _on_console(self, msg) -> None:
        if msg.type in ("error", "warning"):
            logger.warning("[UberSDR page console:%s] %s", msg.type, msg.text)

    def _on_pageerror(self, exc) -> None:
        self._last_page_error = f"Unhandled JS error on UberSDR page: {exc}"
        logger.error(self._last_page_error)
