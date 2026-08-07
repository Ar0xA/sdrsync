"""RigctldClient against a real (fake) TCP server, not just pure parsing --
covers the get_mode() RPRT-error regression, which needs an actual
two-readline socket interaction to reproduce (see test_rigctld_parsing.py
for the pure parser-level tests)."""
import asyncio

from sdrsync.rig.fake_rigctld import start_server
from sdrsync.rig.rigctld import RigctldClient


def test_get_mode_normal_response():
    async def run():
        server, state = await start_server(port=0)
        port = server.sockets[0].getsockname()[1]
        client = RigctldClient("127.0.0.1", port)
        try:
            assert await client.connect() is True
            assert await client.get_mode() == ("USB", 2400)
            assert client.connected is True
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_get_mode_rprt_error_does_not_hang_or_disconnect():
    """A single-line RPRT reply to 'm' must return None promptly, without
    blocking on a second readline until cmd_timeout and without dropping
    the connection -- the real bug this regression test targets."""
    async def run():
        server, state = await start_server(port=0)
        port = server.sockets[0].getsockname()[1]
        state.mode_error = True
        client = RigctldClient("127.0.0.1", port, cmd_timeout=1.0)
        try:
            assert await client.connect() is True

            result = await asyncio.wait_for(client.get_mode(), timeout=0.5)

            assert result is None
            assert client.connected is True  # must not have been closed
            # And the connection must still be usable for the next command --
            # proves no leftover unread line is sitting in the stream.
            assert await client.get_freq() == 14074000
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())
