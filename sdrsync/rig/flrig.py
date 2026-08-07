"""XML-RPC client for flrig's remote-control interface (not rigctld/hamlib).

Read-only: this app only ever reads the transceiver's state
(frequency/mode/PTT) -- sync is one-way, rig -> WebSDR, so only flrig's
`get_*` methods are used, never `set_*`. Wire formats confirmed against
flrig's actual C++ source (github.com/w1hkj/flrig,
src/server/xml_server.cxx), not just its HTML docs, which disagree with
the source on frequency formatting (docs examples show decimals; the
source sends a plain integer string).

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

    async def get_state(self) -> RigState:
        """Convenience: fetch freq/mode/bandwidth/ptt in one call -- 4
        sequential XML-RPC round-trips (vs. rigctld's 3), each
        independently None-tolerant. xmlrpc.client.MultiCall could batch
        these into one HTTP request but was deliberately not used here:
        its result iterator raises per-sub-call faults lazily, which
        doesn't compose cleanly with this file's per-call fail-to-None
        convention, and batching isn't justified at typical
        poll_interval_s defaults (0.2s) unless profiling shows otherwise."""
        freq = await self.get_freq()
        mode = await self.get_mode()
        passband = await self.get_bandwidth()
        ptt = await self.get_ptt()
        return RigState(freq_hz=freq, mode=mode, passband_hz=passband, ptt=ptt)
