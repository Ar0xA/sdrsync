using System.Net;
using System.Net.Sockets;
using SDRSync.Rig;
using Xunit;

namespace SDRSync.Tests;

/// <summary>
/// FlrigClient against a real (fake) XML-RPC server, not just pure parsing
/// -- ported from tests/test_flrig_client.py.
/// </summary>
public class FlrigClientTests
{
    [Fact]
    public async Task GetState_NormalResponse()
    {
        var server = await FakeFlrigServer.StartAsync(port: 0);
        var client = new FlrigClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            var result = await client.GetStateAsync();
            Assert.Equal(server.State.FreqHz, result.FreqHz);
            Assert.Equal(server.State.Mode, result.Mode);
            Assert.Equal(server.State.PassbandHz, result.PassbandHz);
            Assert.False(result.Ptt);
            Assert.True(client.Connected);
        }
        finally
        {
            await client.DisposeAsync();
            await server.DisposeAsync();
        }
    }

    [Fact]
    public async Task GetBandwidth_UsesOnlyElementZeroOfDualDspResponse()
    {
        var server = await FakeFlrigServer.StartAsync(port: 0);
        server.State.PassbandHz = 1800;
        var client = new FlrigClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            Assert.Equal(1800, await client.GetBandwidthAsync());
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
        var server = await FakeFlrigServer.StartAsync(port: 0);
        var client = new FlrigClient("127.0.0.1", server.Port);
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
    public async Task SetFreq_SendsADoubleNotAnIntOverTheWire()
    {
        // Regression test for a live bug: real flrig's rig.set_vfoA is
        // registered with XML-RPC signature "d:d" (confirmed against
        // flrig's own C++ source, src/server/xml_server.cxx, which does
        // `(double)params[0]`) and its bundled XmlRpc++ library enforces
        // that signature strictly by wire type -- sending a plain <i4>
        // serializes as the wrong type and real flrig rejects it outright
        // with Fault -1 "type error", before ever reaching the rig. This
        // fake server doesn't enforce that (see
        // FakeFlrigState.LastSetVfoAArgType's doc comment), so a
        // value-only assertion would pass even with the bug; this checks
        // the actual deserialized wire type instead.
        var server = await FakeFlrigServer.StartAsync(port: 0);
        var client = new FlrigClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            Assert.True(await client.SetFreqAsync(7074000));
            Assert.Equal(typeof(double), server.State.LastSetVfoAArgType);
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
        var server = await FakeFlrigServer.StartAsync(port: 0);
        var client = new FlrigClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            Assert.True(await client.SetModeAsync("LSB", null));
            Assert.Equal("LSB", server.State.Mode);
        }
        finally
        {
            await client.DisposeAsync();
            await server.DisposeAsync();
        }
    }

    [Fact]
    public async Task SetMode_NeverTouchesTheRigsBandwidth()
    {
        // Regression test for a live user report: an earlier version of
        // SetModeAsync() also called flrig's separate rig.set_bandwidth
        // RPC (confirmed to genuinely exist against flrig's own C++
        // source, src/server/xml_server.cxx) whenever a passband was
        // supplied, but the user found any reverse-sync filter change
        // unwelcome, even a "sensible" one -- matching the same
        // "mode-only" ask that made RigctldClient.SetModeAsync always
        // send hamlib's -1 ("leave bandwidth alone") sentinel instead.
        // SetModeAsync() only ever calls rig.set_mode; rig.set_bandwidth
        // is never called at all.
        var server = await FakeFlrigServer.StartAsync(port: 0);
        var client = new FlrigClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            server.State.PassbandHz = 1800; // whatever the user had manually set on the rig
            Assert.True(await client.SetModeAsync("USB", null));
            Assert.Equal("USB", server.State.Mode);
            Assert.Equal(1800, server.State.PassbandHz); // untouched
        }
        finally
        {
            await client.DisposeAsync();
            await server.DisposeAsync();
        }
    }

    [Fact]
    public async Task SetFreq_PermanentRejectionGivesUpAfterBoundedPolling()
    {
        // SetRejected leaves state unchanged forever -- SetFreqAsync must
        // exhaust its bounded poll-until-match attempts and return false,
        // not hang or retry indefinitely.
        var server = await FakeFlrigServer.StartAsync(port: 0);
        server.State.SetRejected = true;
        var client = new FlrigClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            var start = DateTime.UtcNow;
            var resultTask = client.SetFreqAsync(7074000);
            var completed = await Task.WhenAny(resultTask, Task.Delay(TimeSpan.FromSeconds(5)));
            Assert.Same(resultTask, completed);
            var result = await resultTask;
            var elapsed = DateTime.UtcNow - start;
            Assert.False(result);
            Assert.Equal(14074000, server.State.FreqHz); // unchanged
            Assert.True(elapsed < TimeSpan.FromSeconds(3)); // bounded, not hung
        }
        finally
        {
            await client.DisposeAsync();
            await server.DisposeAsync();
        }
    }

    [Fact]
    public async Task SetMode_TransientDelaySucceedsOnceStateCatchesUp()
    {
        // The case that matters most in real use: flrig's cached
        // get_mode() state lags a few polls behind a genuinely-accepted
        // set_mode() call (real CAT-bus turnaround), not a permanent
        // rejection -- the poll-until-match readback loop must ride this
        // out and still report success, not give up on the first stale read.
        var server = await FakeFlrigServer.StartAsync(port: 0);
        server.State.TransientDelayPolls = 2;
        var client = new FlrigClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            Assert.True(await client.SetModeAsync("CW", null));
            Assert.Equal("CW", server.State.Mode);
        }
        finally
        {
            await client.DisposeAsync();
            await server.DisposeAsync();
        }
    }

    [Fact]
    public async Task SetFreq_TransientDelaySucceedsOnceStateCatchesUp()
    {
        var server = await FakeFlrigServer.StartAsync(port: 0);
        server.State.TransientDelayPolls = 2;
        var client = new FlrigClient("127.0.0.1", server.Port);
        try
        {
            Assert.True(await client.ConnectAsync());
            Assert.True(await client.SetFreqAsync(3573000));
            Assert.Equal(3573000, server.State.FreqHz);
        }
        finally
        {
            await client.DisposeAsync();
            await server.DisposeAsync();
        }
    }

    [Fact]
    public async Task EnsureConnected_RecoversAfterMockServerRestart()
    {
        // Closing and rebinding the fake server on the same port must not
        // leave the client permanently broken -- proves the "drop the
        // connection on any failure, rebuild fresh on next
        // EnsureConnectedAsync()" design actually works, not just that it
        // was intended to.
        var server = await FakeFlrigServer.StartAsync(port: 0);
        var port = server.Port;
        var client = new FlrigClient("127.0.0.1", port, cmdTimeoutS: 1.0);
        Assert.True(await client.EnsureConnectedAsync());

        await server.DisposeAsync();

        // Server is gone -- next call must fail cleanly, not hang or throw.
        var stateAfterDeath = await client.GetFreqAsync();
        Assert.Null(stateAfterDeath);
        Assert.False(client.Connected);

        var server2 = await FakeFlrigServer.StartAsync(host: "127.0.0.1", port: port);
        try
        {
            Assert.True(await client.EnsureConnectedAsync());
            Assert.Equal(14074000, await client.GetFreqAsync());
        }
        finally
        {
            await client.DisposeAsync();
            await server2.DisposeAsync();
        }
    }

    [Fact]
    public async Task ConnectAsync_BoundsAHungConnection()
    {
        // A listener that accepts TCP but never speaks HTTP must not leave
        // ConnectAsync() hanging past connect_timeout. Unlike the Python
        // original (which needed this test to guard against a
        // synchronous-xmlrpc.client-on-a-leaked-executor-thread hazard
        // specific to wrapping a blocking library), HttpClient is natively
        // async here, so that specific failure mode doesn't exist in this
        // port -- this test only re-confirms the timeout bound itself still holds.
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        var stop = new CancellationTokenSource();

        var acceptTask = Task.Run(async () =>
        {
            try
            {
                using var conn = await listener.AcceptTcpClientAsync(stop.Token);
                await Task.Delay(Timeout.Infinite, stop.Token);
            }
            catch (OperationCanceledException)
            {
                // expected on test teardown
            }
        });

        try
        {
            var client = new FlrigClient("127.0.0.1", port, connectTimeoutS: 1.0, cmdTimeoutS: 1.0);
            var start = DateTime.UtcNow;
            var ok = await client.ConnectAsync();
            var elapsed = DateTime.UtcNow - start;
            Assert.False(ok);
            Assert.True(elapsed < TimeSpan.FromSeconds(5), $"ConnectAsync took {elapsed.TotalSeconds:F1}s");
        }
        finally
        {
            stop.Cancel();
            listener.Stop();
            await Task.WhenAny(acceptTask, Task.Delay(TimeSpan.FromSeconds(2)));
        }
    }
}
