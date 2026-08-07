"""FlrigClient against a real (fake) XML-RPC server, not just pure parsing
-- covers reconnect-after-failure and TimeoutTransport's actual
timeout-enforcement behavior, which need a real socket to reproduce (see
test_flrig_parsing.py for the pure parser-level tests)."""
import asyncio
import socket
import threading
import time

from sdrsync.rig.fake_flrig import start_server
from sdrsync.rig.flrig import FlrigClient


def test_get_state_normal_response():
    async def run():
        handle, state = await start_server(port=0)
        client = FlrigClient("127.0.0.1", handle.port)
        try:
            assert await client.connect() is True
            result = await client.get_state()
            assert result.freq_hz == state.freq_hz
            assert result.mode == state.mode
            assert result.passband_hz == state.passband_hz
            assert result.ptt is False
            assert client.connected is True
        finally:
            await client.close()
            handle.close()
            await handle.wait_closed()

    asyncio.run(run())


def test_get_bandwidth_uses_only_element_zero_of_dual_dsp_response():
    """End-to-end check (not just the pure parser test) that a dual-DSP-
    style ['lo', 'hi'] response from a real round trip still comes back
    as a single int, not a crash."""
    async def run():
        handle, state = await start_server(port=0)
        # Simulate a dual-DSP rig by overriding the registered handler's
        # data source -- simplest is to just set passband_hz and confirm
        # element 1 (always "") is ignored, matching the mock's actual
        # wire shape; the "both elements populated" case is covered at
        # the pure-parser level in test_flrig_parsing.py.
        state.passband_hz = 1800
        client = FlrigClient("127.0.0.1", handle.port)
        try:
            assert await client.connect() is True
            assert await client.get_bandwidth() == 1800
        finally:
            await client.close()
            handle.close()
            await handle.wait_closed()

    asyncio.run(run())


def test_ensure_connected_recovers_after_mock_server_restart():
    """Closing and rebinding the fake server on the same port must not
    leave the client permanently broken -- proves the 'drop self._proxy
    on any failure, rebuild fresh on next ensure_connected()' design
    actually works, not just that it was intended to."""
    async def run():
        handle, _ = await start_server(port=0)
        port = handle.port
        client = FlrigClient("127.0.0.1", port, cmd_timeout=1.0)
        assert await client.ensure_connected() is True

        handle.close()
        await handle.wait_closed()

        # Server is gone -- next call must fail cleanly, not hang or raise.
        state_after_death = await client.get_freq()
        assert state_after_death is None
        assert client.connected is False

        handle2, _ = await start_server(host="127.0.0.1", port=port)
        try:
            assert await client.ensure_connected() is True
            assert await client.get_freq() == 14074000
        finally:
            await client.close()
            handle2.close()
            await handle2.wait_closed()

    asyncio.run(run())


def test_timeout_transport_bounds_a_hung_connection():
    """A listener that accepts TCP but never speaks HTTP must not leave
    the run_in_executor thread permanently blocked -- direct coverage for
    TimeoutTransport, new/unproven code unlike the well-trodden
    asyncio.wait_for-around-raw-sockets pattern rigctld already uses.

    Deliberately measures the wall-clock time of the *whole* asyncio.run()
    call, not just connect()'s return value: an outer asyncio.wait_for()
    around connect() returns/raises on schedule regardless of whether the
    underlying socket read ever actually unblocks -- cancelling a
    run_in_executor future does not stop the executor thread itself. The
    real hang this guards against is asyncio.run()'s own executor
    shutdown blocking forever waiting for that leaked thread (confirmed
    via a manual repro: with a stock xmlrpc.client.Transport swapped in
    instead of TimeoutTransport, this whole test hangs past 60s). So the
    listener/thread are intentionally NOT torn down until after
    asyncio.run() has fully returned -- tearing them down inside run()'s
    own try/finally would unblock the leaked thread from the outside and
    hide exactly the bug this test exists to catch."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def accept_and_stall():
        listener.settimeout(2.0)
        try:
            conn, _ = listener.accept()
            stop.wait(10.0)
            conn.close()
        except OSError:
            pass

    thread = threading.Thread(target=accept_and_stall, daemon=True)
    thread.start()

    async def run():
        client = FlrigClient("127.0.0.1", port, connect_timeout=1.0, cmd_timeout=1.0)
        return await asyncio.wait_for(client.connect(), timeout=3.0)

    try:
        start = time.monotonic()
        ok = asyncio.run(run())
        elapsed = time.monotonic() - start
        assert ok is False
        # Generous bound (well under the accept thread's 10s stall) --
        # this is what actually fails if TimeoutTransport doesn't free
        # the executor thread: asyncio.run()'s own shutdown would block
        # for the full 10s (or hang outright with a longer stall).
        assert elapsed < 5.0, f"asyncio.run() took {elapsed:.1f}s -- executor thread was not released"
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=2.0)
