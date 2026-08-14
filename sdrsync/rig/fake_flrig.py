"""Minimal fake flrig XML-RPC server, for smoke-testing without real
flrig software.

Registers the RPC methods sdrsync.rig.flrig.FlrigClient actually calls:
main.get_version, rig.get_xcvr, rig.get_vfo, rig.get_vfoA, rig.get_mode,
rig.get_bw, rig.get_ptt, and (v11 reverse sync) rig.set_vfoA, rig.set_mode.
Run standalone
(`python -m sdrsync.rig.fake_flrig`) to get an interactive prompt that
lets you change frequency/mode/PTT while the rest of the app polls this
server exactly like a real flrig instance.

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
        # Mirrors real flrig's xcvr_online flag: rig.get_xcvr (and every
        # get_vfo/get_mode/get_bw/get_ptt call) returns a fixed placeholder
        # instead of live state whenever this is False -- see
        # flrig.py's parse_xcvr_online_response()/_xcvr_online() docstrings
        # for why FlrigClient probes this before trusting a reading.
        # Defaults True so every existing test/manual session behaves like
        # a normal connected rig unless a test explicitly flips it.
        self.xcvr_online = True
        self.xcvr_name = "FakeRig"
        # v11 reverse-sync (WebSDR -> rig) SET testing, mirrors real
        # flrig's own confirmed-unreliable set_vfoA/set_mode RPC
        # responses (see flrig.py's module docstring) -- both test flags
        # below leave the RPC call itself succeeding (no exception),
        # only the resulting *state change* behaves differently, since
        # that's what FlrigClient's poll-until-match readback actually
        # has to contend with.
        #
        # When True, set_vfoA()/set_mode() are accepted but never change
        # state at all -- exercises FlrigClient.set_freq()/set_mode()'s
        # poll-until-match exhaustion/give-up path (a genuinely rejected
        # value, e.g. outside the rig's range).
        self.set_rejected = False
        # When > 0, a set_vfoA()/set_mode() call doesn't apply
        # immediately -- it's staged as pending and only takes effect
        # after this many subsequent get_vfo()/get_mode() polls,
        # simulating real CAT-bus turnaround lag (flrig's own cached
        # state is refreshed by its internal rig-polling loop, not
        # instantaneously on the SET RPC's reply). Exercises the
        # poll-until-match readback's actual success path -- the case
        # that matters most in real use, unlike the permanent-reject
        # flag above.
        self.transient_delay_polls = 0
        self._pending_freq_hz: "int | None" = None
        self._pending_freq_countdown = 0
        self._pending_mode: "str | None" = None
        self._pending_mode_countdown = 0
        # Records the raw Python type xmlrpc.server deserialized the last
        # set_vfoA argument into: a wire <double> becomes a Python float,
        # a wire <int>/<i4> becomes a Python int. Unlike real flrig's
        # XmlRpc++ (see flrig.py's set_freq() comment), this fake server
        # doesn't enforce the "d:d" signature -- it accepts either wire
        # type silently -- so this is the only way a test here can catch
        # a regression back to sending the wrong XML-RPC type, which a
        # value-only assertion (state.freq_hz == ...) can't distinguish.
        self.last_set_vfoA_arg_type: "type | None" = None


class _FlrigXMLRPCServer(SimpleXMLRPCServer):
    # SimpleXMLRPCServer defaults this True; on Windows that lets a
    # second instance silently bind over an already-listening socket
    # (SO_REUSEADDR permits hijacking a live port there, unlike POSIX --
    # confirmed live during the v7 plan review) instead of raising
    # OSError like asyncio.start_server does. False on Windows for that
    # reason.
    #
    # On POSIX, though, SO_REUSEADDR does NOT allow binding over an
    # actively listening socket at all -- two processes still can't both
    # listen on the same port. Its only real effect there is letting a
    # fresh bind succeed while the PREVIOUS socket's already-closed
    # connections are still lingering in TIME_WAIT (~60s) -- exactly the
    # ordinary "Disconnect, then Connect again a few seconds later" case.
    # Leaving this False unconditionally made that ordinary case fail
    # with a misleading "[Errno 98] Address already in use ... Is a real
    # flrig already running on that port?" (confirmed live, and the
    # reason test_ensure_connected_recovers_after_mock_server_restart is
    # flaky when run alongside other tests on Linux).
    allow_reuse_address = sys.platform != "win32"


def _get_xcvr(state: FakeFlrigState) -> str:
    return state.xcvr_name if state.xcvr_online else ""


def _get_vfo(state: FakeFlrigState) -> str:
    # Real flrig's rig.get_vfo returns this exact fixed placeholder while
    # xcvr_online is False, unconditionally, before ever touching the real
    # VFO state -- confirmed against xml_server.cxx. Mirrored here so a
    # test exercising FlrigClient's offline handling sees the same string
    # a real flrig instance would send.
    if not state.xcvr_online:
        return "14070000"
    if state._pending_freq_hz is not None:
        state._pending_freq_countdown -= 1
        if state._pending_freq_countdown <= 0:
            state.freq_hz = state._pending_freq_hz
            state._pending_freq_hz = None
    return str(state.freq_hz)


def _get_mode(state: FakeFlrigState) -> str:
    # Same fixed-placeholder behavior as _get_vfo above, confirmed against
    # the real rig.get_mode handler.
    if not state.xcvr_online:
        return "USB"
    if state._pending_mode is not None:
        state._pending_mode_countdown -= 1
        if state._pending_mode_countdown <= 0:
            state.mode = state._pending_mode
            state._pending_mode = None
    return state.mode


def _set_vfoA(state: FakeFlrigState, freq_hz) -> int:
    # Real flrig's set_vfoA success path never sets a meaningful `result`
    # either -- the RPC call always "succeeds" from the caller's
    # perspective, only the resulting state (or lack of it) differs.
    state.last_set_vfoA_arg_type = type(freq_hz)
    if not state.set_rejected:
        freq_hz = int(freq_hz)
        if state.transient_delay_polls > 0:
            state._pending_freq_hz = freq_hz
            state._pending_freq_countdown = state.transient_delay_polls
        else:
            state.freq_hz = freq_hz
    return 0


def _set_mode(state: FakeFlrigState, mode_name) -> int:
    if not state.set_rejected:
        if state.transient_delay_polls > 0:
            state._pending_mode = mode_name
            state._pending_mode_countdown = state.transient_delay_polls
        else:
            state.mode = mode_name
    return 0


def _register_methods(server: _FlrigXMLRPCServer, state: FakeFlrigState) -> None:
    server.register_function(lambda: FLRIG_MOCK_VERSION, "main.get_version")
    server.register_function(lambda: _get_xcvr(state), "rig.get_xcvr")
    server.register_function(lambda: _get_vfo(state), "rig.get_vfo")
    # rig.get_vfoA: real flrig's handler always does a genuine live CAT
    # read (unlike rig.get_vfo's in-memory cache read -- see flrig.py's
    # set_freq() docstring, fixed by a bug-hunter review pass to poll
    # this instead). This fake server doesn't model separate VFO A/B
    # state at all, so _get_vfo's own pending-countdown simulation of
    # "takes N polls to become visible" already stands in correctly for
    # either RPC from a client's observable poll-until-match behavior.
    server.register_function(lambda: _get_vfo(state), "rig.get_vfoA")
    server.register_function(lambda: _get_mode(state), "rig.get_mode")
    server.register_function(lambda: [str(state.passband_hz), ""], "rig.get_bw")
    server.register_function(lambda: int(state.ptt), "rig.get_ptt")
    server.register_function(lambda freq_hz: _set_vfoA(state, freq_hz), "rig.set_vfoA")
    server.register_function(lambda mode_name: _set_mode(state, mode_name), "rig.set_mode")


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
        elif parts[0] == "x" and len(parts) == 2 and parts[1] in ("0", "1"):
            state.xcvr_online = parts[1] == "1"
            print(f"xcvr_online -> {state.xcvr_online}")
        else:
            print("usage: f <hz> | m <mode> <passband_hz> | t <0|1> | x <0|1> | q")


async def main(host: str = "127.0.0.1", port: int = 12345) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    handle, state = await start_server(host, port)
    print("Commands: f <hz> | m <mode> <passband_hz> | t <0|1> | x <0|1> | q")
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
