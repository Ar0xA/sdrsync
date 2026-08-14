"""XML-RPC client for flrig's remote-control interface (not rigctld/hamlib).

Mostly read-only: get_freq/get_mode/get_bandwidth/get_ptt/get_state drive
the existing rig -> WebSDR forward sync. set_freq/set_mode additionally
support the reverse direction (WebSDR -> rig, v11) via flrig's
`rig.set_vfoA`/`rig.set_mode` RPCs. Never add set_ptt -- PTT is
explicitly excluded from reverse sync (see the v11 plan: an
unauthenticated web page must never be able to key a real transmitter;
rig.set_ptt also blocks up to ~1s server-side retry-polling actual PTT
state, one more reason it's excluded). Wire formats confirmed against
flrig's actual C++ source (github.com/w1hkj/flrig,
src/server/xml_server.cxx), not just its HTML docs, which disagree with
the source on frequency formatting (docs examples show decimals; the
source sends a plain integer string). The source also confirms neither
set_vfoA nor set_mode gives a reliable success signal in their RPC
response (set_vfoA's success path never sets `result`; set_mode silently
no-ops if the mode string doesn't match the rig's own mode list) -- every
reverse push here is verified via a bounded poll-until-match readback of
the corresponding get_*() call instead of trusting the call's own return
value.

xmlrpc.client is synchronous/blocking -- every real RPC call here is
dispatched via loop.run_in_executor so it never blocks the engine's
asyncio event loop, mirroring the "GUI thread never blocks" discipline
already enforced elsewhere in this codebase for wx/WebView2 calls.
"""
from __future__ import annotations

import asyncio
import http.client
import logging
import xmlrpc.client
from typing import Any, Optional
from xml.parsers.expat import ExpatError

from sdrsync.rig.base import RigState

logger = logging.getLogger("sdrsync.flrig")

CONNECT_TIMEOUT_S = 2.0
CMD_TIMEOUT_S = 1.0
RECONNECT_BASE_DELAY_S = 2.0
RECONNECT_MAX_DELAY_S = 30.0
RECONNECT_WARN_AFTER = 5

# Bounded poll-until-match readback budget for set_freq()/set_mode()
# (v11 reverse sync). flrig's own get_vfo/get_mode reflect its internally
# cached, polling-refreshed state, not live hardware state at the instant
# of the set RPC's reply, so a naive immediate single readback would
# report almost every successful push as a failure -- confirmed via
# source during plan review, real CAT-bus turnaround is commonly
# 100-500ms.
#
# Bounded by WALL CLOCK, not by attempt count -- an attempt-count bound
# looks like "5 * 0.15s = ~0.75s" on paper, but each _poll_call() can
# itself take up to cmd_timeout (1.0s) if a poll genuinely times out,
# making the real worst case ~1.0s (initial set) + 5*1.0s (polls) +
# 4*0.15s (sleeps) = ~6.6s of _tick() blocked on one push -- no status
# snapshots, no forward sync, an apparently-frozen GUI, and (if the push
# keeps getting rejected every tick) that cost repeating every tick
# indefinitely. The deadline is checked AFTER each sleep+poll (not
# before), so the loop always does at least one full poll -- the real
# worst case is therefore SET_VERIFY_BUDGET_S + one extra
# SET_VERIFY_POLL_INTERVAL_S + cmd_timeout, not a hard cap at
# SET_VERIFY_BUDGET_S itself.
SET_VERIFY_BUDGET_S = 1.5
SET_VERIFY_POLL_INTERVAL_S = 0.15
FREQ_VERIFY_TOLERANCE_HZ = 10

# Every real XML-RPC call must be wrapped in this so a hung/misbehaving
# server can't hang run_in_executor indefinitely -- asyncio.run()'s
# executor shutdown has no timeout of its own in Python 3.12, confirmed
# by reproducing a 120s+ hang without this during the plan review.
_RPC_ERRORS = (OSError, xmlrpc.client.Error, http.client.HTTPException, ExpatError)


class TimeoutTransport(xmlrpc.client.Transport):
    """Stock xmlrpc.client.ServerProxy has no timeout= kwarg. Transport
    caches/reuses one HTTPConnection per host; make_connection() returns
    it before any request is sent, so setting .timeout here (once, right
    after construction) is safe and covers both the connect and read
    phases of every request made through it -- coarser-grained than
    rigctld's separate connect/cmd timeouts, but there's no finer control
    available through the public API."""

    def __init__(self, timeout: float) -> None:
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def parse_freq_response(resp: Any) -> Optional[int]:
    """Pure parser for rig.get_vfo's response, e.g. '14070000' (plain
    integer Hz string, confirmed via source -- no decimal point)."""
    if isinstance(resp, str) and resp.strip().lstrip("-").isdigit():
        return int(resp.strip())
    return None


def parse_mode_response(resp: Any) -> Optional[str]:
    """Pure parser for rig.get_mode's response, e.g. 'USB'. Unlike
    rigctld's 'm' command, mode and bandwidth are separate RPCs here --
    this returns just the mode string."""
    if isinstance(resp, str) and resp.strip():
        return resp.strip()
    return None


def parse_bandwidth_response(resp: Any) -> Optional[int]:
    """Pure parser for rig.get_bw's response: a 2-element array of
    strings. For most rigs (single bandwidth table) element 0 holds the
    value and element 1 is ''; for rigs with dual DSP lo/hi controls,
    both elements hold separate lo/hi values. Only element 0 is used --
    this app only needs one passband_hz number, same as rigctld already
    provides. The underlying per-rig table is a generic label list, not
    guaranteed to be a clean numeric string for every rig flrig
    supports, so this is deliberately defensive."""
    if not isinstance(resp, (list, tuple)) or len(resp) < 1:
        return None
    value = resp[0]
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def parse_ptt_response(resp: Any) -> Optional[bool]:
    """Pure parser for rig.get_ptt's response: a plain XML-RPC int 0/1."""
    if isinstance(resp, bool) or not isinstance(resp, int):
        return None
    return resp != 0


def parse_xcvr_online_response(resp: Any) -> bool:
    """Pure parser for rig.get_xcvr's response: the transceiver's name
    when flrig has a live serial link to it, or "" under the EXACT same
    `!xcvr_online || disable_xmlrpc->value()` guard (confirmed against
    flrig's own source, src/server/xml_server.cxx) that makes
    get_vfo/get_mode/get_bw fall back to safe placeholder values ("14070000"
    Hz, etc.) instead of erroring -- so those placeholders are otherwise
    indistinguishable from a genuine reading of a rig that's actually
    offline (powered off, cable unplugged, or flrig's own XML-RPC
    toggle switched off)."""
    return isinstance(resp, str) and resp != ""


class FlrigClient:
    def __init__(
        self,
        host: str,
        port: int,
        connect_timeout: float = CONNECT_TIMEOUT_S,
        cmd_timeout: float = CMD_TIMEOUT_S,
        reconnect_base_delay: float = RECONNECT_BASE_DELAY_S,
        reconnect_max_delay: float = RECONNECT_MAX_DELAY_S,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.cmd_timeout = cmd_timeout
        self.reconnect_base_delay = reconnect_base_delay
        self.reconnect_max_delay = reconnect_max_delay

        self._proxy: Optional[xmlrpc.client.ServerProxy] = None
        self._reconnect_failures = 0
        # Edge-tracked so "flrig is up but the transceiver is offline"
        # logs once per transition, not every poll tick.
        self._xcvr_was_online = True

    @property
    def connected(self) -> bool:
        return self._proxy is not None

    async def close(self) -> None:
        # xmlrpc.client.ServerProxy has no public close/teardown method --
        # dropping the reference lets its cached HTTPConnection be
        # garbage-collected. No blocking I/O to await here, unlike
        # RigctldClient.close()'s writer.wait_closed().
        self._proxy = None

    async def connect(self) -> bool:
        await self.close()
        proxy = xmlrpc.client.ServerProxy(
            f"http://{self.host}:{self.port}/RPC2",
            transport=TimeoutTransport(self.cmd_timeout),
        )
        loop = asyncio.get_running_loop()
        try:
            # main.get_version is flrig's one precondition-free call --
            # confirmed via source it doesn't depend on a rig being
            # attached, unlike get_vfo/get_mode/get_bw/get_ptt (which
            # fall back to safe placeholder values instead of erroring).
            # Probing with it here catches a broken connection immediately
            # rather than on the first real poll call.
            await asyncio.wait_for(
                loop.run_in_executor(None, proxy.main.get_version), timeout=self.connect_timeout
            )
        except (*_RPC_ERRORS, asyncio.TimeoutError):
            return False
        self._proxy = proxy
        return True

    def reconnect_delay(self) -> float:
        """Exponential backoff delay for the *next* reconnect attempt."""
        return min(
            self.reconnect_base_delay * (2 ** self._reconnect_failures),
            self.reconnect_max_delay,
        )

    async def ensure_connected(self) -> bool:
        """Connect if needed; returns True if a usable connection exists afterwards.

        Caller is responsible for sleeping reconnect_delay() between calls
        when this returns False (the sync engine's poll loop already sleeps
        each tick, so it composes naturally).
        """
        if self.connected:
            return True
        ok = await self.connect()
        if ok:
            if self._reconnect_failures > 0:
                logger.info("Reconnected to flrig after %d failed attempt(s)", self._reconnect_failures)
            self._reconnect_failures = 0
            return True
        self._reconnect_failures += 1
        if self._reconnect_failures % RECONNECT_WARN_AFTER == 0:
            logger.warning(
                "%d consecutive failed attempts to connect to flrig at %s:%d",
                self._reconnect_failures, self.host, self.port,
            )
        else:
            logger.debug("flrig connection attempt %d failed", self._reconnect_failures)
        return False

    async def _call(self, method) -> Any:
        """Dispatch one already-bound XML-RPC method (e.g. self._proxy.rig.get_vfo)
        via the executor, with a hard timeout. None on any failure -- also
        drops the connection, since Transport caches/reuses the underlying
        HTTPConnection across calls and a failure proves it's no longer
        trustworthy; the next ensure_connected() rebuilds a fresh one."""
        if self._proxy is None:
            return None
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(None, method), timeout=self.cmd_timeout)
        except (*_RPC_ERRORS, asyncio.TimeoutError) as e:
            logger.warning("Lost connection to flrig (%s)", e)
            await self.close()
            return None

    async def get_freq(self) -> Optional[int]:
        if self._proxy is None:
            return None
        resp = await self._call(self._proxy.rig.get_vfo)
        return parse_freq_response(resp)

    async def get_mode(self) -> Optional[str]:
        """Just the mode string -- unlike RigctldClient.get_mode(), flrig's
        rig.get_mode and rig.get_bw are independent RPCs, not a combined
        response. get_state() is what unifies both into one RigState."""
        if self._proxy is None:
            return None
        resp = await self._call(self._proxy.rig.get_mode)
        return parse_mode_response(resp)

    async def get_bandwidth(self) -> Optional[int]:
        if self._proxy is None:
            return None
        resp = await self._call(self._proxy.rig.get_bw)
        return parse_bandwidth_response(resp)

    async def get_ptt(self) -> Optional[bool]:
        if self._proxy is None:
            return None
        resp = await self._call(self._proxy.rig.get_ptt)
        ptt = parse_ptt_response(resp)
        if ptt is None and resp is not None:
            logger.warning("Unexpected PTT value from flrig: %r", resp)
        return ptt

    # Note: the `if self._proxy is None` guards above are redundant with
    # _call()'s own check -- kept anyway so `self._proxy.rig.get_vfo`
    # (building the bound-method reference) never runs against a None
    # proxy and raises AttributeError before _call() gets a chance to
    # short-circuit.

    async def _poll_call(self, build_call) -> Any:
        """Like _call(), but never drops the connection on a single failed
        attempt -- used only by set_freq()/set_mode()'s bounded
        poll-until-match readback loop, where one slow/timed-out poll
        among several retries should not force a full reconnect (a real
        connection loss will still surface via the *next* ensure_connected()
        cycle's own probing, same as any other tick)."""
        if self._proxy is None:
            return None
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(None, build_call), timeout=self.cmd_timeout)
        except (*_RPC_ERRORS, asyncio.TimeoutError):
            return None

    async def set_freq(self, freq_hz: int, verify_budget_s: Optional[float] = None) -> bool:
        """Reverse-sync (WebSDR -> rig). See module docstring: set_vfoA's
        RPC response is not a reliable success signal, so this verifies
        via a bounded poll-until-match readback of get_freq() instead.
        Bounded by wall clock, not attempt count -- see SET_VERIFY_BUDGET_S's
        comment for why. verify_budget_s overrides the module default
        (used by sync/engine.py's reverse-sync retry ladder, which wants a
        shorter per-attempt budget and retries several times itself rather
        than one long wait). Also refuses outright if the transceiver
        itself is offline (see _xcvr_online()'s docstring) -- without
        this, set_vfoA's own readback would poll the same "14070000" Hz
        placeholder get_state() already knows to distrust, never confirm
        the requested value, and after REVERSE_PUSH_MAX_ATTEMPTS the
        engine's retry ladder would give up and revert the WebSDR page
        back onto that placeholder -- fighting the operator's real tuning
        for no reason."""
        if self._proxy is None or not await self._xcvr_online():
            return False
        freq_hz = int(freq_hz)
        budget = SET_VERIFY_BUDGET_S if verify_budget_s is None else verify_budget_s
        # rig.set_vfoA's registered XML-RPC signature is "d:d" (confirmed
        # against flrig's actual source, src/server/xml_server.cxx: the
        # handler casts params[0] via `(double)params[0]`). XmlRpc++ (the
        # bundled library flrig uses) enforces the registered signature
        # strictly by the XML-RPC type TAG, not just the numeric value --
        # passing a plain Python int here serializes as <int> and flrig
        # rejects it outright with Fault -1 "type error" before ever
        # reaching the rig, confirmed live. float() forces xmlrpc.client
        # to serialize it as <double> instead.
        await self._call(lambda: self._proxy.rig.set_vfoA(float(freq_hz)))
        deadline = asyncio.get_running_loop().time() + budget
        while True:
            await asyncio.sleep(SET_VERIFY_POLL_INTERVAL_S)
            if self._proxy is None:
                break
            # rig.get_vfoA, NOT rig.get_vfo -- a bug-hunter review pass
            # found this verify loop was reading rig.get_vfo, whose
            # handler (confirmed against flrig's real source,
            # src/server/xml_server.cxx) just returns whichever of
            # vfoA.freq/vfoB.freq is cached in memory (no live CAT read
            # at all -- updated only by flrig's own periodic serial-poll
            # thread or a prior set_vfoA call), so a poll landing before
            # that background thread's next cycle could echo the very
            # value THIS set_vfoA call just wrote to that same cache,
            # "confirming" a push regardless of whether the physical rig
            # ever actually took it. rig.get_vfoA's handler instead calls
            # `selrig->get_vfoA()` unconditionally -- a genuine live CAT
            # read every call -- and is also the correct VFO to check in
            # the first place: set_vfoA always targets VFO A specifically,
            # while get_vfo reads "whichever VFO flrig currently has in
            # use" (vfoB.freq during a split/dual-VFO operation), a
            # second, independent mismatch this also happens to fix.
            resp = await self._poll_call(lambda: self._proxy.rig.get_vfoA())
            actual = parse_freq_response(resp)
            if actual is not None and abs(actual - freq_hz) <= FREQ_VERIFY_TOLERANCE_HZ:
                return True
            if asyncio.get_running_loop().time() >= deadline:
                break
        logger.warning("flrig did not confirm set_vfoA(%d) within %.2fs", freq_hz, budget)
        return False

    async def set_mode(self, mode_name: str, verify_budget_s: Optional[float] = None) -> bool:
        """Reverse-sync (WebSDR -> rig). Calls ONLY rig.set_mode -- this
        driver never itself calls rig.set_bandwidth (confirmed against
        flrig's source, src/server/xml_server.cxx: registered signature
        "i:i"; an earlier version of this method called it, but a user
        reported live that having reverse sync touch the filter at all
        was unwelcome, matching the same "mode-only" ask that changed
        RigctldClient.set_mode() to always send hamlib's -1 ("leave
        bandwidth alone") sentinel instead of a concrete width -- see
        that method's docstring).

        This is NOT actually equivalent to rigctld's -1 sentinel, though
        -- flrig has no "leave bandwidth alone" input at all (set_bandwidth's
        own 0 clamps to the NARROWEST supported filter, confirmed via
        source), and more importantly rig.set_mode's OWN server-side
        handler resets the filter to that mode's default internally
        regardless of what this driver calls (confirmed against flrig's
        real source, src/server/xml_server.cxx's rig_set_mode class and
        src/support/support.cxx's serviceA(): `nuvals.iBW =
        selrig->def_bandwidth(i)`, applied via set_bwA() if it differs
        from the current filter). So a reverse-sync mode change on flrig
        DOES change your filter width to that mode's flrig-configured
        default -- there is no way to avoid this via flrig's XML-RPC
        surface at all, not just something this driver chooses not to
        do. Found by a bug-hunter review pass; left as-is (not restoring
        the prior bandwidth via an explicit set_bandwidth call
        afterward) per explicit user direction, given the earlier
        set_bandwidth-touching version was already found unwelcome once.

        Verified via a bounded poll-until-match readback of get_mode() --
        set_mode's own RPC response silently no-ops on an unrecognized
        mode string rather than erroring. verify_budget_s: see
        set_freq() -- including the same xcvr-offline refusal.

        Unlike set_freq()'s verify loop (fixed to use rig.get_vfoA, a
        genuine live CAT read every call), there is no live-read
        equivalent available for mode: confirmed against flrig's real
        source that BOTH rig.get_mode and rig.get_modeA just read an
        in-memory struct field (vfo->imode / vfoA.imode) with no CAT
        query at all -- updated only by flrig's own periodic serial-poll
        thread or a prior set_mode call, same staleness risk as the old
        rig.get_vfo. get_mode (not get_modeA) is still the right one to
        poll, though: rig.set_mode targets "whichever VFO flrig
        currently has in use" (serviceA/serviceB based on
        selrig->inuse), which is exactly what get_mode reads back --
        get_modeA would be reading the wrong VFO during a split/dual-VFO
        operation. This staleness window is real but narrower in
        practice than set_freq()'s was: SET_VERIFY_POLL_INTERVAL_S
        (250ms) is close to flrig's own serial-poll cadence, so most
        polls land on an already-refreshed value; found by a bug-hunter
        review pass, left as a known, documented limitation rather than
        a false claim of full verification."""
        if self._proxy is None or not await self._xcvr_online():
            return False
        budget = SET_VERIFY_BUDGET_S if verify_budget_s is None else verify_budget_s
        await self._call(lambda: self._proxy.rig.set_mode(mode_name))
        deadline = asyncio.get_running_loop().time() + budget
        while True:
            await asyncio.sleep(SET_VERIFY_POLL_INTERVAL_S)
            if self._proxy is None:
                break
            resp = await self._poll_call(lambda: self._proxy.rig.get_mode())
            actual = parse_mode_response(resp)
            if actual is not None and actual == mode_name:
                return True
            if asyncio.get_running_loop().time() >= deadline:
                break
        logger.warning("flrig did not confirm set_mode(%r) within %.2fs", mode_name, budget)
        return False

    async def _xcvr_online(self) -> bool:
        """True if flrig reports a live transceiver -- see
        parse_xcvr_online_response's docstring for why this probe exists:
        without it, an offline rig's fixed freq/mode placeholders
        ("14070000" Hz / "USB", confirmed via source) are silently
        indistinguishable from genuine readings, and the sync engine
        would happily retune a public WebSDR to them. rig.get_ptt is a
        separate risk of its own: it is NOT gated by xcvr_online in
        flrig's source at all, so it keeps returning whatever PTT state
        it last tracked -- possibly stale and no longer meaningful once
        the transceiver connection is actually gone. Treating the whole
        reading as untrustworthy while offline covers both cases."""
        if self._proxy is None:
            return False
        resp = await self._call(self._proxy.rig.get_xcvr)
        online = parse_xcvr_online_response(resp)
        if online != self._xcvr_was_online:
            if online:
                logger.info("flrig reports the transceiver back online")
            else:
                logger.warning(
                    "flrig is connected but reports no transceiver online "
                    "(rig off/unplugged, or flrig's own XML-RPC toggle is "
                    "disabled) -- pausing sync until it reappears"
                )
            self._xcvr_was_online = online
        return online

    async def get_state(self) -> RigState:
        """Convenience: fetch freq/mode/bandwidth/ptt in one call -- 4
        sequential XML-RPC round-trips (vs. rigctld's 3), each
        independently None-tolerant. xmlrpc.client.MultiCall could batch
        these into one HTTP request but was deliberately not used here:
        its result iterator raises per-sub-call faults lazily, which
        doesn't compose cleanly with this file's per-call fail-to-None
        convention, and batching isn't justified at typical
        poll_interval_s defaults (0.2s) unless profiling shows otherwise.

        Probes _xcvr_online() first (a 5th round-trip) and returns an
        all-None RigState if the transceiver isn't actually there -- every
        caller in sync/engine.py already treats a None field as "nothing
        to sync" (state.freq_hz is not None / state.mode is not None /
        the documented state.ptt is None fallback), so this needs no
        further special-casing upstream; it just stops a fabricated
        placeholder reading from being trusted as real."""
        if not await self._xcvr_online():
            return RigState(freq_hz=None, mode=None, passband_hz=None, ptt=None)
        freq = await self.get_freq()
        mode = await self.get_mode()
        passband = await self.get_bandwidth()
        ptt = await self.get_ptt()
        return RigState(freq_hz=freq, mode=mode, passband_hz=passband, ptt=ptt)
