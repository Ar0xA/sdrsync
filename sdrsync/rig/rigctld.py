"""Async TCP client for hamlib's rigctld (network CAT control, not Omnirig).

Read-only: this app only ever reads the transceiver's state
(frequency/mode/PTT) — sync is one-way, rig -> WebSDR, so no "set
frequency on rig" command is needed here.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("sdrsync.rigctld")

CONNECT_TIMEOUT_S = 2.0
CMD_TIMEOUT_S = 1.0
RECONNECT_BASE_DELAY_S = 2.0
RECONNECT_MAX_DELAY_S = 30.0
RECONNECT_WARN_AFTER = 5

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


@dataclass
class RigState:
    freq_hz: Optional[int]
    mode: Optional[str]
    passband_hz: Optional[int]
    ptt: Optional[bool]


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
        """Send a command, read a single response line. None on I/O failure (and disconnects)."""
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

    async def get_state(self) -> RigState:
        """Convenience: fetch freq/mode/ptt in one call. Any of them may be None on failure."""
        freq = await self.get_freq()
        mode_pb = await self.get_mode()
        ptt = await self.get_ptt()
        mode, passband = mode_pb if mode_pb else (None, None)
        return RigState(freq_hz=freq, mode=mode, passband_hz=passband, ptt=ptt)
