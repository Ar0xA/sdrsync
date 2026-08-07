"""check_flrig() against a real fake_flrig mock server, and against a
closed port -- mirrors the existing rigctld preflight behavior (see
sdrsync/preflight.py's check_rigctld, which has no dedicated test file
today, so no existing pattern to mirror exactly)."""
import asyncio
import socket

from sdrsync.preflight import check_flrig
from sdrsync.rig.fake_flrig import start_server


def test_check_flrig_success():
    async def run():
        handle, _ = await start_server(port=0)
        try:
            ok, message = await check_flrig("127.0.0.1", handle.port)
            assert ok is True
            assert "reachable" in message
        finally:
            handle.close()
            await handle.wait_closed()

    asyncio.run(run())


def test_check_flrig_failure_on_closed_port():
    async def run():
        # Bind and immediately release a port, so it's very likely nothing
        # is listening there for the duration of the check.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        ok, message = await check_flrig("127.0.0.1", port, timeout=1.0)

        assert ok is False
        assert "Could not reach flrig" in message

    asyncio.run(run())
