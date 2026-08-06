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

logger = logging.getLogger("sdrsync.fake_rigctld")


class FakeRigState:
    def __init__(self) -> None:
        self.freq_hz = 14074000
        self.mode = "USB"
        self.passband_hz = 2400
        self.ptt = "0"


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
                writer.write(f"{state.freq_hz}\n".encode())
            elif cmd == "m":
                writer.write(f"{state.mode}\n{state.passband_hz}\n".encode())
            elif cmd == "t":
                writer.write(f"{state.ptt}\n".encode())
            elif cmd == "v":
                writer.write(b"Dummy\n")
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
