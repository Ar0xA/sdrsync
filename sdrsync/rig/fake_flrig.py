"""Minimal fake flrig XML-RPC server, for smoke-testing without real
flrig software.

Registers just the 5 RPC methods sdrsync.rig.flrig.FlrigClient actually
calls: main.get_version, rig.get_vfo, rig.get_mode, rig.get_bw,
rig.get_ptt. Run standalone (`python -m sdrsync.rig.fake_flrig`) to get
an interactive prompt that lets you change frequency/mode/PTT while the
rest of the app polls this server exactly like a real flrig instance.

Threading note: xmlrpc.server.SimpleXMLRPCServer is a synchronous
socketserver.TCPServer with no native asyncio integration (unlike
fake_rigctld.py's asyncio.start_server, whose handler coroutines run on
the engine's own event-loop thread). So this runs serve_forever() in a
dedicated background daemon thread -- meaning, unlike fake_rigctld.py,
FakeFlrigState's attributes are read from that background thread (inside
the registered RPC handlers) while SyncEngine.push_mock_freq/mode/ptt
write them from the engine's event-loop thread via call_soon_threadsafe.
This relies on CPython's GIL making individual plain int/str attribute
get/set atomic. push_mock_mode does write mode and passband_hz together,
but that's still safe here: rig.get_mode and rig.get_bw are separate RPC
methods/handlers (mirroring real flrig, which has no combined "mode+
bandwidth" call either), so a client reading one mid-update and the
other after it sees exactly the same kind of momentarily-stale pairing
it would see against a real flrig instance -- not a new failure mode
this mock introduces. What the GIL reliance actually guards is simpler:
no single attribute read/write ever tears mid-operation. Don't add a
field to FakeFlrigState whose *own* value needs multi-step consistency
(e.g. something computed from two other fields) without reconsidering
this.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from xmlrpc.server import SimpleXMLRPCServer

logger = logging.getLogger("sdrsync.fake_flrig")

FLRIG_MOCK_VERSION = "fake-flrig-1.0"


class FakeFlrigState:
    def __init__(self) -> None:
        self.freq_hz = 14074000
        self.mode = "USB"
        self.passband_hz = 2400
        self.ptt = "0"


class _FlrigXMLRPCServer(SimpleXMLRPCServer):
    # SimpleXMLRPCServer defaults this True; on Windows that lets a
    # second instance silently bind over an already-listening socket
    # (SO_REUSEADDR permits hijacking a live port there, unlike POSIX --
    # confirmed live during the v7 plan review) instead of raising
    # OSError like asyncio.start_server does. Explicitly False so a
    # port-in-use mistake surfaces as the same friendly error
    # SyncEngine._start_rig already handles for the rigctld mock.
    allow_reuse_address = False


def _register_methods(server: _FlrigXMLRPCServer, state: FakeFlrigState) -> None:
    server.register_function(lambda: FLRIG_MOCK_VERSION, "main.get_version")
    server.register_function(lambda: str(state.freq_hz), "rig.get_vfo")
    server.register_function(lambda: state.mode, "rig.get_mode")
    server.register_function(lambda: [str(state.passband_hz), ""], "rig.get_bw")
    server.register_function(lambda: int(state.ptt), "rig.get_ptt")


class FlrigMockServerHandle:
    """Async-compatible wrapper around the threaded SimpleXMLRPCServer --
    presents the same close() + await wait_closed() shape SyncEngine
    already expects from an asyncio.AbstractServer (used for the rigctld
    mock), so _stop_rig() needs no backend-specific handling."""

    def __init__(self, server: _FlrigXMLRPCServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def close(self) -> None:
        # TCPServer.shutdown() is thread-safe but *blocks the calling
        # thread* until serve_forever()'s poll loop notices (bounded by
        # its poll_interval). Calling it directly here would stall
        # SyncEngine's whole asyncio loop for that long on every rig
        # stop/reconnect, since close() runs on that loop's thread --
        # dispatch it to a short-lived helper thread instead so this
        # method returns immediately, matching the non-blocking contract
        # close() already has for the real asyncio.AbstractServer case.
        threading.Thread(target=self._server.shutdown, daemon=True).start()

    async def wait_closed(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._thread.join)
        self._server.server_close()


async def start_server(host: str = "127.0.0.1", port: int = 12345) -> "tuple[FlrigMockServerHandle, FakeFlrigState]":
    """Reusable entry point: also used by SyncEngine to embed a mock
    flrig directly when AppSettings.use_mock_rig is set and rig_backend
    is "flrig". Raises OSError (e.g. address already in use) rather than
    swallowing it -- callers should surface that as a real, visible
    error. Binding happens synchronously in __init__, so this can raise
    before any thread is started, same as fake_rigctld.start_server."""
    state = FakeFlrigState()
    server = _FlrigXMLRPCServer((host, port), logRequests=False, allow_none=True)
    _register_methods(server, state)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    logger.info("Fake flrig listening on %s:%d", host, server.server_address[1])
    return FlrigMockServerHandle(server, thread), state


async def _repl(state: FakeFlrigState) -> None:
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
        elif parts[0] == "t" and len(parts) == 2 and parts[1] in ("0", "1"):
            # Validated (unlike fake_rigctld.py's REPL) because rig.get_ptt's
            # handler does int(state.ptt) -- a non-numeric value here would
            # raise inside the RPC handler on every subsequent poll, not
            # just get parsed as "unknown" the way rigctld's text protocol
            # would tolerate it.
            state.ptt = parts[1]
            print(f"ptt -> {state.ptt}")
        else:
            print("usage: f <hz> | m <mode> <passband_hz> | t <0|1> | q")


async def main(host: str = "127.0.0.1", port: int = 12345) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    handle, state = await start_server(host, port)
    print("Commands: f <hz> | m <mode> <passband_hz> | t <0|1> | q")
    try:
        await _repl(state)
    finally:
        handle.close()
        await handle.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
