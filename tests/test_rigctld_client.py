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


def test_set_freq_success_applies_and_confirms():
    async def run():
        server, state = await start_server(port=0)
        port = server.sockets[0].getsockname()[1]
        client = RigctldClient("127.0.0.1", port)
        try:
            assert await client.connect() is True
            assert await client.set_freq(7074000) is True
            assert state.freq_hz == 7074000
            assert await client.get_freq() == 7074000
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_set_mode_success_applies_and_confirms():
    async def run():
        server, state = await start_server(port=0)
        port = server.sockets[0].getsockname()[1]
        client = RigctldClient("127.0.0.1", port)
        try:
            assert await client.connect() is True
            assert await client.set_mode("LSB", 1800) is True
            assert state.mode == "LSB"
            assert state.passband_hz == 1800
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_set_mode_zero_passband_means_rig_default():
    async def run():
        server, state = await start_server(port=0)
        port = server.sockets[0].getsockname()[1]
        client = RigctldClient("127.0.0.1", port)
        try:
            assert await client.connect() is True
            assert await client.set_mode("CW", None) is True
            assert state.mode == "CW"
            assert state.passband_hz == 0
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_set_freq_rejected_by_rig():
    async def run():
        server, state = await start_server(port=0)
        port = server.sockets[0].getsockname()[1]
        state.set_rejected = True
        client = RigctldClient("127.0.0.1", port)
        try:
            assert await client.connect() is True
            assert await client.set_freq(7074000) is False
            assert client.connected is True  # a rejection is not a disconnect
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_set_freq_retries_until_confirmed_after_dropped_cat_command():
    """'RPRT 0' alone isn't trusted -- a command the rig silently dropped
    (acked but not applied) must be caught by the readback verification
    and retried, not reported as success on the first ack."""
    async def run():
        server, state = await start_server(port=0)
        port = server.sockets[0].getsockname()[1]
        state.freq_set_ignore_count = 1  # first F is ack'd but dropped
        client = RigctldClient("127.0.0.1", port)
        try:
            assert await client.connect() is True
            assert await client.set_freq(7074000) is True
            assert state.freq_hz == 7074000
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_set_mode_retries_until_confirmed_after_dropped_cat_command():
    async def run():
        server, state = await start_server(port=0)
        port = server.sockets[0].getsockname()[1]
        state.mode_set_ignore_count = 1
        client = RigctldClient("127.0.0.1", port)
        try:
            assert await client.connect() is True
            assert await client.set_mode("LSB", 1800) is True
            assert state.mode == "LSB"
            assert state.passband_hz == 1800
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_set_freq_gives_up_after_max_attempts_when_never_confirmed():
    """A command the rig keeps silently dropping every attempt must give
    up after SET_VERIFY_MAX_ATTEMPTS, not retry forever."""
    async def run():
        server, state = await start_server(port=0)
        port = server.sockets[0].getsockname()[1]
        state.freq_set_ignore_count = 999  # never actually applies
        client = RigctldClient("127.0.0.1", port)
        try:
            assert await client.connect() is True
            assert await client.set_freq(7074000) is False
            assert state.freq_hz == 14074000  # untouched -- never applied
            assert client.connected is True  # exhausting retries is not a disconnect
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_set_mode_gives_up_after_max_attempts_when_never_confirmed():
    async def run():
        server, state = await start_server(port=0)
        port = server.sockets[0].getsockname()[1]
        state.mode_set_ignore_count = 999
        client = RigctldClient("127.0.0.1", port)
        try:
            assert await client.connect() is True
            assert await client.set_mode("LSB", 1800) is False
            assert state.mode == "USB"  # untouched -- never applied
            assert client.connected is True
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())
