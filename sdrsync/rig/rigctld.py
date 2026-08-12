"""Async TCP client for hamlib's rigctld (network CAT control, not Omnirig).

Mostly read-only: get_freq/get_mode/get_ptt/get_state drive the existing
rig -> WebSDR forward sync. set_freq/set_mode additionally support the
reverse direction (WebSDR -> rig, v11) via rigctld's symmetric uppercase
SET commands ('F <hz>', 'M <mode> <passband>'), each replying with the
same single 'RPRT <code>' line shape as the GET side's error path. Never
add set_ptt -- PTT is explicitly excluded from reverse sync (see the v11
plan: an unauthenticated web page must never be able to key a real
transmitter).

'RPRT 0' only confirms rigctld accepted the command syntactically, not
that the physical rig actually took it -- CAT-bus commands can be
dropped or collide, especially over serial links, and this was observed
to happen in practice (mode pushes "often but not always" landing).
set_freq/set_mode therefore verify via a poll-until-match readback after
sending, polling repeatedly for up to SET_VERIFY_BUDGET_S before giving
up and returning False -- mirrors FlrigClient's existing poll-until-
match verification. Wall-clock bounded, not attempt-count bounded: an
earlier attempt-count version (3 tries, 200ms apart, ~0.6s total) was
observed live to give up too early on real hardware -- the rig had
genuinely accepted the command and applied it correctly, just slower
than the budget allowed, producing a false "rejected" warning. The
command is sent once, not resent on each poll -- rigctld already
acknowledged it (RPRT 0); resending while it may still be landing risks
confusing rigs that don't tolerate overlapping CAT commands, and every
sign so far points to "slow to apply", not "silently dropped".

A verify-loop readback poll that times out under CAT-bus congestion
still closes the connection, same as any other command timeout -- an
earlier version of this fix tried leaving the connection open on a
verify-poll timeout instead (to stop a single slow poll from also
stalling the forward sync direction until the next tick's
ensure_connected() noticed), but that is unsafe for this line-oriented
protocol: asyncio.wait_for()'s cancellation does not discard the
in-flight read, so if the "late" reply eventually arrives it sits in the
StreamReader's buffer and gets handed to whatever command is sent next,
permanently misattributing replies from that point on (confirmed via a
live repro against a stub server that answers correctly but slowly just
once). set_freq()/set_mode()'s verify loops instead reconnect
IMMEDIATELY after a poll-timeout closes the connection (see
_reconnect_if_dropped()) -- a fresh TCP connection can never have a
stray buffered reply, so this gets both properties: no corrupted
stream, and no full tick lost waiting for ensure_connected() to notice.

The verify loop also always sleeps once before its first readback poll
(never polls at t=0) -- an immediate poll fires in the same instant the
SET command was written, when the rig is certain not to have applied it
yet, and is the poll most likely to collide with that command on the bus.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from sdrsync.rig.base import RigState

logger = logging.getLogger("sdrsync.rigctld")

CONNECT_TIMEOUT_S = 2.0
CMD_TIMEOUT_S = 1.0
RECONNECT_BASE_DELAY_S = 2.0
RECONNECT_MAX_DELAY_S = 30.0
RECONNECT_WARN_AFTER = 5

# set_freq()/set_mode() readback-verification cadence -- see the module
# docstring for why this is wall-clock bounded rather than attempt-count
# bounded. Mirrors FlrigClient's SET_VERIFY_BUDGET_S/
# SET_VERIFY_POLL_INTERVAL_S. This module default is only a fallback --
# in practice sync/engine.py's reverse-sync ladder always overrides it
# via verify_budget_s (see REVERSE_PUSH_ATTEMPT_VERIFY_S).
# The budget bounds the polling loop's own wall clock, not the call's
# total duration: a readback poll that times out costs up to one
# cmd_timeout and closes the connection, and _reconnect_if_dropped()
# then costs up to a further connect_timeout rebuilding it -- so a
# pathological set_freq()/set_mode() can overrun the stated budget by
# roughly cmd_timeout + connect_timeout (~3s with the defaults).
SET_VERIFY_BUDGET_S = 1.0
SET_VERIFY_POLL_INTERVAL_S = 0.25
# abs(Hz) tolerance for treating a frequency readback as "confirmed" --
# mirrors FlrigClient.FREQ_VERIFY_TOLERANCE_HZ (some rigs' CAT layers
# round to a step size rather than echoing back the exact requested Hz).
FREQ_VERIFY_TOLERANCE_HZ = 10

# PTT values 1 (mic), 2 (data)... hamlib backends vary; all non-zero means "transmitting".
PTT_TX_VALUES = {"1", "2", "3"}


def parse_freq_response(resp: Optional[str]) -> Optional[int]:
    """Pure parser for rigctld's 'f' response, e.g. '14074000'."""
    if resp and resp.strip().lstrip("-").isdigit():
        return int(resp.strip())
    return None


def parse_mode_response(mode_line: str, passband_line: str) -> Optional[tuple[str, int]]:
    """Pure parser for rigctld's two-line 'm' response: mode name, then passband Hz."""
    mode = mode_line.strip()
    pb_str = passband_line.strip()
    if not mode or not pb_str.lstrip("-").isdigit():
        return None
    return mode, int(pb_str)


def is_rprt_error(line: str) -> bool:
    """True if a rigctld response line is an 'RPRT <code>' error reply.

    On error, rigctld sends a single RPRT line instead of the normal
    response — for 'm' that means only one line comes back, not two.
    Reading a second line in that case blocks until cmd_timeout and forces
    a disconnect, so callers must check this after the first readline.
    """
    return line.strip().startswith("RPRT")


def parse_set_response(line: Optional[str]) -> bool:
    """Pure parser for a rigctld SET command's 'RPRT <code>' reply.

    True only for 'RPRT 0' (success); False for any other code, malformed
    reply, or None (I/O failure already logged by the caller)."""
    if line is None:
        return False
    parts = line.strip().split()
    return len(parts) == 2 and parts[0] == "RPRT" and parts[1] == "0"


def parse_ptt_response(resp: Optional[str]) -> Optional[bool]:
    """Pure parser for rigctld's 't' response: 0=RX, 1/2/3=TX (mic/data variants)."""
    if resp is None:
        return None
    resp = resp.strip()
    if resp in PTT_TX_VALUES:
        return True
    if resp == "0":
        return False
    return None


class RigctldClient:
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

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._reconnect_failures = 0

    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def close(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    async def connect(self) -> bool:
        await self.close()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=self.connect_timeout
            )
            return True
        except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
            return False

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
                logger.info("Reconnected to rigctld after %d failed attempt(s)", self._reconnect_failures)
            self._reconnect_failures = 0
            return True
        self._reconnect_failures += 1
        if self._reconnect_failures % RECONNECT_WARN_AFTER == 0:
            logger.warning(
                "%d consecutive failed attempts to connect to rigctld at %s:%d",
                self._reconnect_failures, self.host, self.port,
            )
        else:
            logger.debug("rigctld connection attempt %d failed", self._reconnect_failures)
        return False

    async def _send_raw(self, cmd: str) -> Optional[str]:
        """Send a command, read a single response line. None on I/O failure
        (and disconnects) -- always closes the connection on a timeout, no
        exceptions (see the module docstring for why a verify-loop poll
        timeout must NOT be treated differently here; that loop instead
        reconnects immediately afterward, via _reconnect_if_dropped())."""
        if not self._writer or not self._reader:
            return None
        try:
            self._writer.write(f"{cmd}\n".encode())
            await self._writer.drain()
            line = await asyncio.wait_for(self._reader.readline(), timeout=self.cmd_timeout)
            return line.decode(errors="replace").strip()
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError, OSError):
            logger.warning("Lost connection to rigctld while sending %r", cmd)
            await self.close()
            return None

    async def get_freq(self) -> Optional[int]:
        resp = await self._send_raw("f")
        return parse_freq_response(resp)

    async def get_mode(self) -> Optional[tuple[str, int]]:
        """rigctld's 'm' command normally replies with two lines: mode name,
        then passband in Hz. On error it replies with a single 'RPRT <code>'
        line instead -- reading a second line in that case would block until
        cmd_timeout, so that's checked for before attempting it."""
        if not self._writer or not self._reader:
            return None
        try:
            self._writer.write(b"m\n")
            await self._writer.drain()
            mode_line = await asyncio.wait_for(self._reader.readline(), timeout=self.cmd_timeout)
            mode_line_str = mode_line.decode(errors="replace")
            if is_rprt_error(mode_line_str):
                logger.debug("rigctld returned an error for 'm': %r", mode_line_str.strip())
                return None
            pb_line = await asyncio.wait_for(self._reader.readline(), timeout=self.cmd_timeout)
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError, OSError):
            logger.warning("Lost connection to rigctld while sending 'm'")
            await self.close()
            return None
        return parse_mode_response(mode_line_str, pb_line.decode(errors="replace"))

    async def get_ptt(self) -> Optional[bool]:
        resp = await self._send_raw("t")
        ptt = parse_ptt_response(resp)
        if ptt is None and resp is not None:
            logger.warning("Unexpected PTT value from rigctld: %r", resp)
        return ptt

    async def _reconnect_if_dropped(self) -> None:
        """Called after a verify-loop readback poll -- if that poll's
        timeout just closed the connection (get_freq()/get_mode() always
        close on timeout), reconnect immediately rather than leaving it
        closed until the next _tick()'s ensure_connected() notices, which
        would otherwise also cost the FORWARD sync direction a full poll
        interval. A fresh TCP connection also guarantees no stray
        buffered reply is left over to desync a later command's response
        -- see the module docstring for why that matters."""
        if not self.connected:
            await self.connect()

    async def set_freq(self, freq_hz: int, verify_budget_s: Optional[float] = None) -> bool:
        """Reverse-sync (WebSDR -> rig): sets the rig's VFO frequency.

        'RPRT 0' only confirms rigctld accepted the command, not that the
        physical rig actually took it -- verified via a poll-until-match
        readback of get_freq() (see module docstring for why this is
        wall-clock bounded and doesn't resend the command on each poll).
        verify_budget_s overrides the module default SET_VERIFY_BUDGET_S
        -- used by sync/engine.py's reverse-sync retry ladder, which wants
        a short per-attempt budget and retries several times itself
        rather than one long wait."""
        freq_hz = int(freq_hz)
        budget = SET_VERIFY_BUDGET_S if verify_budget_s is None else verify_budget_s
        resp = await self._send_raw(f"F {freq_hz}")
        if not parse_set_response(resp):
            logger.warning("rigctld rejected F %d: %r", freq_hz, resp)
            return False
        deadline = asyncio.get_running_loop().time() + budget
        while True:
            await asyncio.sleep(SET_VERIFY_POLL_INTERVAL_S)
            actual = await self.get_freq()
            if actual is not None and abs(actual - freq_hz) <= FREQ_VERIFY_TOLERANCE_HZ:
                return True
            if actual is None:
                await self._reconnect_if_dropped()
            if asyncio.get_running_loop().time() >= deadline:
                break
        logger.warning(
            "rigctld accepted F %d but the rig never confirmed it within %.1fs",
            freq_hz, budget,
        )
        return False

    async def set_mode(self, mode_name: str, verify_budget_s: Optional[float] = None) -> bool:
        """Reverse-sync (WebSDR -> rig): sets ONLY the rig's mode, never
        its filter/passband width -- the WebSDR page has no reliable way
        to say which specific filter width it wants (WebSDRStatus.mode is
        already base-collapsed, e.g. narrow/wide USB both just report
        "USB"), and a user reported live that having reverse sync touch
        the filter at all was unwelcome even when a "sensible" value was
        being sent, e.g. the rig's own current width or its mode default.
        rigctld's 'M' command has a real "leave bandwidth alone" input for
        exactly this: -1, distinct from 0 ("use the rig's default for
        this mode") -- confirmed via hamlib's own rigctld documentation,
        not assumed. Every reverse-sync mode push now sends -1
        unconditionally.

        Verified via a poll-until-match readback of get_mode(), same
        reasoning (and cadence) as set_freq() above. verify_budget_s: see
        set_freq()."""
        budget = SET_VERIFY_BUDGET_S if verify_budget_s is None else verify_budget_s
        resp = await self._send_raw(f"M {mode_name} -1")
        if not parse_set_response(resp):
            logger.warning("rigctld rejected M %s -1: %r", mode_name, resp)
            return False
        deadline = asyncio.get_running_loop().time() + budget
        while True:
            await asyncio.sleep(SET_VERIFY_POLL_INTERVAL_S)
            confirmed = await self.get_mode()
            actual_mode = confirmed[0] if confirmed else None
            if actual_mode == mode_name:
                return True
            if confirmed is None:
                await self._reconnect_if_dropped()
            if asyncio.get_running_loop().time() >= deadline:
                break
        logger.warning(
            "rigctld accepted M %s -1 but the rig never confirmed it within %.1fs",
            mode_name, budget,
        )
        return False

    async def get_state(self) -> RigState:
        """Convenience: fetch freq/mode/ptt in one call. Any of them may be None on failure."""
        freq = await self.get_freq()
        mode_pb = await self.get_mode()
        ptt = await self.get_ptt()
        mode, passband = mode_pb if mode_pb else (None, None)
        return RigState(freq_hz=freq, mode=mode, passband_hz=passband, ptt=ptt)
