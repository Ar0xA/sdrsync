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
            assert await client.set_mode("LSB") is True
            assert state.mode == "LSB"
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_set_mode_never_touches_the_rigs_passband():
    """Regression test for a live user report: reverse-sync mode changes
    used to also set the rig's filter/passband (0 = "rig default" is a
    real hamlib value, not "leave alone"), which the user found
    unwelcome even though a "sensible" value was being sent. set_mode()
    now always sends hamlib's real -1 ("leave bandwidth alone") sentinel
    -- confirmed via hamlib's own rigctld documentation -- so the rig's
    passband must be completely unaffected by a mode change, whatever it
    was set to beforehand."""
    async def run():
        server, state = await start_server(port=0)
        port = server.sockets[0].getsockname()[1]
        state.passband_hz = 1800  # whatever the user had manually set on the rig
        client = RigctldClient("127.0.0.1", port)
        try:
            assert await client.connect() is True
            assert await client.set_mode("CW") is True
            assert state.mode == "CW"
            assert state.passband_hz == 1800  # untouched
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


def test_set_freq_polls_through_slow_cat_bus_turnaround():
    """'RPRT 0' alone isn't trusted -- a command the rig is still catching
    up on (accepted, not yet visible in readback) must be caught by the
    poll-until-match verification and confirmed once it lands, not
    reported as success on the very first (stale) readback."""
    async def run():
        server, state = await start_server(port=0)
        port = server.sockets[0].getsockname()[1]
        state.freq_apply_after_polls = 1  # first readback still stale
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


def test_set_mode_polls_through_slow_cat_bus_turnaround():
    async def run():
        server, state = await start_server(port=0)
        port = server.sockets[0].getsockname()[1]
        state.mode_apply_after_polls = 1
        client = RigctldClient("127.0.0.1", port)
        try:
            assert await client.connect() is True
            assert await client.set_mode("LSB") is True
            assert state.mode == "LSB"
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_set_freq_gives_up_after_verify_budget_when_never_confirmed():
    """A command the rig never actually applies must give up once the
    wall-clock verify budget is exhausted, not poll forever."""
    async def run():
        server, state = await start_server(port=0)
        port = server.sockets[0].getsockname()[1]
        state.freq_apply_after_polls = 999  # never actually applies
        client = RigctldClient("127.0.0.1", port)
        try:
            assert await client.connect() is True
            assert await client.set_freq(7074000) is False
            assert state.freq_hz == 14074000  # untouched -- never applied
            assert client.connected is True  # exhausting the budget is not a disconnect
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_set_mode_gives_up_after_verify_budget_when_never_confirmed():
    async def run():
        server, state = await start_server(port=0)
        port = server.sockets[0].getsockname()[1]
        state.mode_apply_after_polls = 999
        client = RigctldClient("127.0.0.1", port)
        try:
            assert await client.connect() is True
            assert await client.set_mode("LSB") is False
            assert state.mode == "USB"  # untouched -- never applied
            assert client.connected is True
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


async def _start_raw_server(never_respond_to: str):
    """A raw (non-fake_rigctld) TCP server that answers 'RPRT 0' to any SET
    command but never responds at all to whatever command's first token is
    in never_respond_to -- used to make a real socket readline() timeout
    happen, which fake_rigctld's always-promptly-answering mock can't
    reproduce (it can only simulate a stale-but-present value)."""
    async def handle(reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                cmd = line.decode(errors="replace").strip().split()
                if not cmd:
                    continue
                if cmd[0] == never_respond_to:
                    continue  # deliberately no response -- forces a client-side timeout
                writer.write(b"RPRT 0\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _start_delayed_freq_reply_server(delay_s: float):
    """A raw TCP server that answers 'F ...' immediately (RPRT 0) and 'm'
    immediately ('USB'/'2400'), but delays its reply to the FIRST 'f' by
    delay_s -- models a genuinely congested-but-still-working CAT bus (the
    reply eventually arrives correct, just late), not a dead one.
    fake_rigctld can't reproduce this (always answers promptly)."""
    state = {"delayed_once": False}

    async def handle(reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                cmd = line.decode(errors="replace").strip().split()
                if not cmd:
                    continue
                if cmd[0] == "F":
                    writer.write(b"RPRT 0\n")
                elif cmd[0] == "m":
                    writer.write(b"USB\n2400\n")
                elif cmd[0] == "f":
                    if not state["delayed_once"]:
                        state["delayed_once"] = True
                        await asyncio.sleep(delay_s)
                    writer.write(b"14074000\n")
                else:
                    writer.write(b"RPRT -11\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def test_normal_get_freq_timeout_still_drops_the_connection():
    """Baseline/contrast for the next test: a timeout on an ORDINARY
    get_freq() call must still drop the connection, same as before v12 --
    the verify loops don't get any different close-on-timeout treatment,
    they just reconnect immediately afterward (see the next test).

    Note: deliberately doesn't await server.wait_closed() -- a raw stub
    server whose lone handler is left mid-readline() when the client
    disconnects hits a known Windows ProactorEventLoop quirk where
    wait_closed() can hang indefinitely even after that connection
    actually closes (confirmed via a standalone repro during this test's
    own development, unrelated to RigctldClient's own logic). server.close()
    alone is enough here since each test binds a fresh ephemeral port."""
    async def run():
        server, port = await _start_raw_server(never_respond_to="f")
        client = RigctldClient("127.0.0.1", port, cmd_timeout=0.15)
        try:
            assert await client.connect() is True
            assert await client.get_freq() is None
            assert client.connected is False
        finally:
            await client.close()
            server.close()

    asyncio.run(run())


def test_set_freq_verify_poll_timeout_reconnects_without_corrupting_the_stream():
    """v12 regression coverage for a real bug an Opus implementation
    review found in an EARLIER version of this fix: leaving the
    connection open (not closing) on a verify-poll timeout is unsafe for
    this line-oriented protocol -- asyncio.wait_for()'s cancellation
    doesn't discard the in-flight read, so if the "late" reply eventually
    arrives (a merely-slow, not dead, connection) it sits in the
    StreamReader's buffer and gets handed to whatever command is sent
    NEXT, permanently misattributing replies from that point on. The
    fix that replaced it: close on timeout (as always) AND reconnect
    IMMEDIATELY inside the verify loop, so a later command always runs
    against a fresh connection that structurally cannot have a stray
    buffered reply.

    Server answers the first 'f' correctly but delayed past cmd_timeout,
    so the client's poll times out on it (closing that connection) while
    the correct-but-late reply is still in flight on the OLD socket. If
    the client had instead just left that connection open, this delayed
    reply would land in its buffer and get misread as the reply to the
    'm' sent below -- proven by asserting the 'm' reply comes back
    correctly parsed, not corrupted by the stray '14074000' line.

    Note: doesn't await server.wait_closed() -- see the previous test's
    docstring."""
    async def run():
        server, port = await _start_delayed_freq_reply_server(delay_s=0.4)
        client = RigctldClient("127.0.0.1", port, cmd_timeout=0.15)
        try:
            assert await client.connect() is True
            ok = await client.set_freq(14_074_000, verify_budget_s=0.3)
            assert ok is False  # the one poll attempt times out before the delayed reply arrives

            # Give the OLD connection's now-stale delayed reply time to
            # actually try to land (and be silently discarded, since the
            # client already closed that socket) before proceeding.
            await asyncio.sleep(0.5)
            assert client.connected is True  # reconnected automatically, not left dead

            mode = await client.get_mode()
            assert mode == ("USB", 2400)  # correct, freshly-requested reply -- not the stray '14074000'
        finally:
            await client.close()
            server.close()

    asyncio.run(run())
