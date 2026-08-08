"""Minimal fake rigctld TCP server, for smoke-testing without real hardware.

Speaks just enough of hamlib's rigctld text protocol for this app: f, m, t, v.
Run standalone (`python -m sdrsync.rig.fake_rigctld`) to get an interactive
prompt that lets you change frequency/mode/PTT while the rest of the app
polls this server exactly like a real rigctld instance.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

logger = logging.getLogger("sdrsync.fake_rigctld")


class FakeRigState:
    def __init__(self) -> None:
        self.freq_hz = 14074000
        self.mode = "USB"
        self.passband_hz = 2400
        self.ptt = "0"
        # When set, 'm' replies with a single RPRT error line instead of the
        # normal two-line mode/passband response -- lets tests exercise
        # RigctldClient.get_mode()'s handling of that case without a real rig.
        self.mode_error = False
        # When set, 'F'/'M' (v11 reverse-sync SET commands) reply with
        # RPRT -11 (rejected) instead of applying the change and replying
        # RPRT 0 -- lets tests exercise RigctldClient.set_freq()/set_mode()'s
        # rejection handling without a real rig.
        self.set_rejected = False
        # Number of upcoming 'f'/'m' GET polls to keep reporting the OLD
        # value after an accepted 'F'/'M' SET command, before the new
        # value becomes visible -- simulates real CAT-bus turnaround lag
        # (the command genuinely landed, it just takes the physical rig
        # a moment to catch up), which is what set_freq()/set_mode()'s
        # poll-until-match verification budget exists to ride out. Set to
        # a huge number to simulate a command that never actually lands
        # (tests the give-up path) rather than one that's merely slow.
        # Independent per field, decremented only by GET polls of that
        # field.
        self.freq_apply_after_polls = 0
        self.mode_apply_after_polls = 0
        self._pending_freq_hz: Optional[int] = None
        self._pending_mode: Optional[str] = None
        self._pending_passband_hz: Optional[int] = None


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, state: FakeRigState) -> None:
    peer = writer.get_extra_info("peername")
    logger.info("Client connected: %s", peer)
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            cmd = line.decode(errors="replace").strip()
            if cmd == "f":
                if state._pending_freq_hz is not None:
                    if state.freq_apply_after_polls > 0:
                        state.freq_apply_after_polls -= 1
                    else:
                        state.freq_hz = state._pending_freq_hz
                        state._pending_freq_hz = None
                writer.write(f"{state.freq_hz}\n".encode())
            elif cmd == "m":
                if state._pending_mode is not None:
                    if state.mode_apply_after_polls > 0:
                        state.mode_apply_after_polls -= 1
                    else:
                        state.mode = state._pending_mode
                        state.passband_hz = state._pending_passband_hz
                        state._pending_mode = None
                        state._pending_passband_hz = None
                if state.mode_error:
                    writer.write(b"RPRT -11\n")
                else:
                    writer.write(f"{state.mode}\n{state.passband_hz}\n".encode())
            elif cmd == "t":
                writer.write(f"{state.ptt}\n".encode())
            elif cmd == "v":
                writer.write(b"Dummy\n")
            elif cmd.startswith("F "):
                parts = cmd.split()
                if state.set_rejected or len(parts) != 2 or not parts[1].lstrip("-").isdigit():
                    writer.write(b"RPRT -11\n")
                else:
                    state._pending_freq_hz = int(parts[1])
                    writer.write(b"RPRT 0\n")
            elif cmd.startswith("M "):
                parts = cmd.split()
                if state.set_rejected or len(parts) != 3 or not parts[2].lstrip("-").isdigit():
                    writer.write(b"RPRT -11\n")
                else:
                    state._pending_mode = parts[1]
                    state._pending_passband_hz = int(parts[2])
                    writer.write(b"RPRT 0\n")
            else:
                writer.write(b"RPRT -11\n")
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        logger.info("Client disconnected: %s", peer)
        writer.close()


async def _repl(state: FakeRigState) -> None:
    """Interactive console: 'f <hz>', 'm <mode> <passband>', 't <0|1>', 'q' to quit."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except Exception:
            break
        if not line:
            break
        parts = line.strip().split()
        if not parts:
            continue
        if parts[0] == "q":
            break
        if parts[0] == "f" and len(parts) == 2 and parts[1].isdigit():
            state.freq_hz = int(parts[1])
            print(f"freq -> {state.freq_hz}")
        elif parts[0] == "m" and len(parts) == 3:
            state.mode = parts[1]
            state.passband_hz = int(parts[2])
            print(f"mode -> {state.mode} / {state.passband_hz}")
        elif parts[0] == "t" and len(parts) == 2:
            state.ptt = parts[1]
            print(f"ptt -> {state.ptt}")
        else:
            print("usage: f <hz> | m <mode> <passband_hz> | t <0|1> | q")


async def start_server(host: str = "127.0.0.1", port: int = 4532) -> "tuple[asyncio.AbstractServer, FakeRigState]":
    """Reusable entry point: also used by SyncEngine to embed a mock rig
    directly in its own asyncio loop when AppSettings.use_mock_rig is set.
    Raises OSError (e.g. address already in use) rather than swallowing it --
    callers should surface that as a real, visible error."""
    state = FakeRigState()
    server = await asyncio.start_server(lambda r, w: _handle_client(r, w, state), host, port)
    logger.info("Fake rigctld listening on %s:%d", host, port)
    return server, state


async def main(host: str = "127.0.0.1", port: int = 4532) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    server, state = await start_server(host, port)
    print("Commands: f <hz> | m <mode> <passband_hz> | t <0|1> | q")
    async with server:
        serve_task = asyncio.ensure_future(server.serve_forever())
        await _repl(state)
        serve_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
