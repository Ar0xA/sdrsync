using System.Net;
using System.Net.Sockets;
using System.Text;
using SDRSync.Rig;
using Xunit;

namespace SDRSync.Tests;

/// <summary>
/// RigctldClient against a real (fake) TCP server, not just pure parsing --
/// ported from tests/test_rigctld_client.py.
/// </summary>
public class RigctldClientTests
{
    [Fact]
    public async Task GetMode_NormalResponse()
    {
        var server = await FakeRigctldServer.StartAsync(port: 0);
        var client = new RigctldClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            Assert.Equal(("USB", 2400), await client.GetModeAsync());
            Assert.True(client.Connected);
        }
        finally
        {
            await client.DisposeAsync();
            await server.DisposeAsync();
        }
    }

    [Fact]
    public async Task GetMode_RprtErrorDoesNotHangOrDisconnect()
    {
        // A single-line RPRT reply to 'm' must return null promptly,
        // without blocking on a second readline until cmd_timeout and
        // without dropping the connection -- the real bug this regression
        // test targets.
        var server = await FakeRigctldServer.StartAsync(port: 0);
        server.State.ModeError = true;
        var client = new RigctldClient("127.0.0.1", server.Port, cmdTimeoutS: 1.0);
        try
        {
            Assert.True(await client.ConnectAsync());

            var resultTask = client.GetModeAsync();
            var completed = await Task.WhenAny(resultTask, Task.Delay(TimeSpan.FromSeconds(0.5)));
            Assert.Same(resultTask, completed);

            Assert.Null(await resultTask);
            Assert.True(client.Connected); // must not have been closed

            // And the connection must still be usable for the next command
            // -- proves no leftover unread line is sitting in the stream.
            Assert.Equal(14074000, await client.GetFreqAsync());
        }
        finally
        {
            await client.DisposeAsync();
            await server.DisposeAsync();
        }
    }

    [Fact]
    public async Task SetFreq_SuccessAppliesAndConfirms()
    {
        var server = await FakeRigctldServer.StartAsync(port: 0);
        var client = new RigctldClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            Assert.True(await client.SetFreqAsync(7074000));
            Assert.Equal(7074000, server.State.FreqHz);
            Assert.Equal(7074000, await client.GetFreqAsync());
        }
        finally
        {
            await client.DisposeAsync();
            await server.DisposeAsync();
        }
    }

    [Fact]
    public async Task SetMode_SuccessAppliesAndConfirms()
    {
        var server = await FakeRigctldServer.StartAsync(port: 0);
        var client = new RigctldClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            Assert.True(await client.SetModeAsync("LSB", 1800));
            Assert.Equal("LSB", server.State.Mode);
        }
        finally
        {
            await client.DisposeAsync();
            await server.DisposeAsync();
        }
    }

    [Fact]
    public async Task SetMode_NeverTouchesTheRigsPassband()
    {
        // Regression test for a live user report: reverse-sync mode
        // changes used to also set the rig's filter/passband (0 = "rig
        // default" is a real hamlib value, not "leave alone"), which the
        // user found unwelcome even though a "sensible" value was being
        // sent. SetModeAsync() now always sends hamlib's real -1 ("leave
        // bandwidth alone") sentinel -- confirmed via hamlib's own
        // rigctld documentation -- so the rig's passband must be
        // completely unaffected by a mode change, whatever it was set to
        // beforehand, and regardless of whatever passbandHz is passed in.
        var server = await FakeRigctldServer.StartAsync(port: 0);
        server.State.PassbandHz = 1800; // whatever the user had manually set on the rig
        var client = new RigctldClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            Assert.True(await client.SetModeAsync("CW", null));
            Assert.Equal("CW", server.State.Mode);
            Assert.Equal(1800, server.State.PassbandHz); // untouched
        }
        finally
        {
            await client.DisposeAsync();
            await server.DisposeAsync();
        }
    }

    [Fact]
    public async Task SetFreq_RejectedByRig()
    {
        var server = await FakeRigctldServer.StartAsync(port: 0);
        server.State.SetRejected = true;
        var client = new RigctldClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            Assert.False(await client.SetFreqAsync(7074000));
            Assert.True(client.Connected); // a rejection is not a disconnect
        }
        finally
        {
            await client.DisposeAsync();
            await server.DisposeAsync();
        }
    }

    [Fact]
    public async Task SetFreq_PollsThroughSlowCatBusTurnaround()
    {
        // "RPRT 0" alone isn't trusted -- a command the rig is still
        // catching up on (accepted, not yet visible in readback) must be
        // caught by the poll-until-match verification and confirmed once
        // it lands, not reported as success on the very first (stale) readback.
        var server = await FakeRigctldServer.StartAsync(port: 0);
        server.State.FreqApplyAfterPolls = 1; // first readback still stale
        var client = new RigctldClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            Assert.True(await client.SetFreqAsync(7074000));
            Assert.Equal(7074000, server.State.FreqHz);
        }
        finally
        {
            await client.DisposeAsync();
            await server.DisposeAsync();
        }
    }

    [Fact]
    public async Task SetMode_PollsThroughSlowCatBusTurnaround()
    {
        var server = await FakeRigctldServer.StartAsync(port: 0);
        server.State.ModeApplyAfterPolls = 1;
        var client = new RigctldClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            Assert.True(await client.SetModeAsync("LSB", 1800));
            Assert.Equal("LSB", server.State.Mode);
        }
        finally
        {
            await client.DisposeAsync();
            await server.DisposeAsync();
        }
    }

    [Fact]
    public async Task SetFreq_GivesUpAfterVerifyBudgetWhenNeverConfirmed()
    {
        // A command the rig never actually applies must give up once the
        // wall-clock verify budget is exhausted, not poll forever.
        var server = await FakeRigctldServer.StartAsync(port: 0);
        server.State.FreqApplyAfterPolls = 999; // never actually applies
        var client = new RigctldClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            Assert.False(await client.SetFreqAsync(7074000));
            Assert.Equal(14074000, server.State.FreqHz); // untouched -- never applied
            Assert.True(client.Connected); // exhausting the budget is not a disconnect
        }
        finally
        {
            await client.DisposeAsync();
            await server.DisposeAsync();
        }
    }

    [Fact]
    public async Task SetMode_GivesUpAfterVerifyBudgetWhenNeverConfirmed()
    {
        var server = await FakeRigctldServer.StartAsync(port: 0);
        server.State.ModeApplyAfterPolls = 999;
        var client = new RigctldClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            Assert.False(await client.SetModeAsync("LSB", 1800));
            Assert.Equal("USB", server.State.Mode); // untouched -- never applied
            Assert.True(client.Connected);
        }
        finally
        {
            await client.DisposeAsync();
            await server.DisposeAsync();
        }
    }

    /// <summary>
    /// A raw (non-fake-server) TCP server that answers "RPRT 0" to any SET
    /// command but never responds at all to whatever command's first token
    /// is in neverRespondTo -- used to make a real socket read timeout
    /// happen, which the always-promptly-answering fake server can't
    /// reproduce (it can only simulate a stale-but-present value).
    /// </summary>
    private static async Task<(TcpListener Listener, int Port)> StartRawServerAsync(string neverRespondTo)
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;

        // Loops accepting connections (not a single accept) -- a verify-poll
        // timeout makes RigctldClient reconnect mid-test, and nothing would
        // be listening for that second connection otherwise. Mirrors
        // asyncio.start_server's behavior of spawning a fresh handler per
        // accepted connection, unlike a single AcceptTcpClientAsync() call.
        _ = Task.Run(async () =>
        {
            try
            {
                while (true)
                {
                    var client = await listener.AcceptTcpClientAsync();
                    _ = Task.Run(async () =>
                    {
                        try
                        {
                            using (client)
                            await using (var stream = client.GetStream())
                            {
                                var reader = new StreamReader(stream, Encoding.ASCII);
                                while (true)
                                {
                                    var line = await reader.ReadLineAsync();
                                    if (line is null) return;
                                    var cmd = line.Trim().Split(' ', StringSplitOptions.RemoveEmptyEntries);
                                    if (cmd.Length == 0) continue;
                                    if (cmd[0] == neverRespondTo) continue; // deliberately no response
                                    var bytes = Encoding.ASCII.GetBytes("RPRT 0\n");
                                    await stream.WriteAsync(bytes);
                                }
                            }
                        }
                        catch (Exception e) when (e is IOException or SocketException or ObjectDisposedException)
                        {
                            // client disconnected
                        }
                    });
                }
            }
            catch (Exception e) when (e is SocketException or ObjectDisposedException)
            {
                // listener stopped
            }
        });

        return (listener, port);
    }

    /// <summary>
    /// A raw TCP server that answers "F ..." immediately (RPRT 0) and "m"
    /// immediately ("USB"/"2400"), but delays its reply to the FIRST "f" by
    /// delay -- models a genuinely congested-but-still-working CAT bus (the
    /// reply eventually arrives correct, just late), not a dead one. The
    /// fake server can't reproduce this (always answers promptly).
    /// </summary>
    private static async Task<(TcpListener Listener, int Port)> StartDelayedFreqReplyServerAsync(TimeSpan delay)
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        var delayedOnce = false;

        // Loops accepting connections -- see StartRawServerAsync's comment
        // for why a single accept isn't enough (the client reconnects onto
        // a NEW connection after the delayed "f" reply's poll times out).
        _ = Task.Run(async () =>
        {
            try
            {
                while (true)
                {
                    var client = await listener.AcceptTcpClientAsync();
                    _ = Task.Run(async () =>
                    {
                        try
                        {
                            using (client)
                            await using (var stream = client.GetStream())
                            {
                                var reader = new StreamReader(stream, Encoding.ASCII);
                                while (true)
                                {
                                    var line = await reader.ReadLineAsync();
                                    if (line is null) return;
                                    var cmd = line.Trim().Split(' ', StringSplitOptions.RemoveEmptyEntries);
                                    if (cmd.Length == 0) continue;

                                    string response;
                                    if (cmd[0] == "F")
                                    {
                                        response = "RPRT 0\n";
                                    }
                                    else if (cmd[0] == "m")
                                    {
                                        response = "USB\n2400\n";
                                    }
                                    else if (cmd[0] == "f")
                                    {
                                        if (!delayedOnce)
                                        {
                                            delayedOnce = true;
                                            await Task.Delay(delay);
                                        }

                                        response = "14074000\n";
                                    }
                                    else
                                    {
                                        response = "RPRT -11\n";
                                    }

                                    var bytes = Encoding.ASCII.GetBytes(response);
                                    await stream.WriteAsync(bytes);
                                }
                            }
                        }
                        catch (Exception e) when (e is IOException or SocketException or ObjectDisposedException)
                        {
                            // client disconnected
                        }
                    });
                }
            }
            catch (Exception e) when (e is SocketException or ObjectDisposedException)
            {
                // listener stopped
            }
        });

        return (listener, port);
    }

    [Fact]
    public async Task NormalGetFreqTimeout_StillDropsTheConnection()
    {
        // Baseline/contrast for the next test: a timeout on an ORDINARY
        // GetFreqAsync() call must still drop the connection -- the verify
        // loops don't get any different close-on-timeout treatment, they
        // just reconnect immediately afterward (see the next test).
        var (listener, port) = await StartRawServerAsync(neverRespondTo: "f");
        var client = new RigctldClient("127.0.0.1", port, cmdTimeoutS: 0.15);
        try
        {
            Assert.True(await client.ConnectAsync());
            Assert.Null(await client.GetFreqAsync());
            Assert.False(client.Connected);
        }
        finally
        {
            await client.DisposeAsync();
            listener.Stop();
        }
    }

    [Fact]
    public async Task SetFreq_VerifyPollTimeoutReconnectsWithoutCorruptingTheStream()
    {
        // Regression coverage for a real bug class: leaving the connection
        // open (not closing) on a verify-poll timeout is unsafe for this
        // line-oriented protocol -- a cancelled read doesn't discard bytes
        // already delivered into the OS socket buffer, so if the "late"
        // reply eventually arrives (a merely-slow, not dead, connection) it
        // sits in the buffer and gets handed to whatever command is sent
        // NEXT, permanently misattributing replies from that point on. The
        // fix: close on timeout (as always) AND reconnect IMMEDIATELY
        // inside the verify loop, so a later command always runs against a
        // fresh connection that structurally cannot have a stray buffered
        // reply.
        //
        // Server answers the first 'f' correctly but delayed past
        // cmd_timeout, so the client's poll times out on it (closing that
        // connection) while the correct-but-late reply is still in flight
        // on the OLD socket. If the client had instead just left that
        // connection open, this delayed reply would land in its buffer and
        // get misread as the reply to the 'm' sent below -- proven by
        // asserting the 'm' reply comes back correctly parsed, not
        // corrupted by the stray "14074000" line.
        var (listener, port) = await StartDelayedFreqReplyServerAsync(TimeSpan.FromSeconds(0.4));
        var client = new RigctldClient("127.0.0.1", port, cmdTimeoutS: 0.15);
        try
        {
            Assert.True(await client.ConnectAsync());
            var ok = await client.SetFreqAsync(14_074_000, verifyBudgetS: 0.3);
            Assert.False(ok); // the one poll attempt times out before the delayed reply arrives

            // Give the OLD connection's now-stale delayed reply time to
            // actually try to land (and be silently discarded, since the
            // client already closed that socket) before proceeding.
            await Task.Delay(TimeSpan.FromSeconds(0.5));
            Assert.True(client.Connected); // reconnected automatically, not left dead

            var mode = await client.GetModeAsync();
            Assert.Equal(("USB", 2400), mode); // correct, freshly-requested reply -- not the stray "14074000"
        }
        finally
        {
            await client.DisposeAsync();
            listener.Stop();
        }
    }
}
