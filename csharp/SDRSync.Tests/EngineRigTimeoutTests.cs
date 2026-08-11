using SDRSync.Core;
using SDRSync.Sync;
using Xunit;

namespace SDRSync.Tests;

/// <summary>
/// Ported from tests/test_engine_rig_timeout.py. An unreachable rig must
/// eventually give up instead of leaving the GUI stuck at "connecting..."
/// forever -- and the connect budget only applies to the time-to-FIRST-
/// connect, not every reconnect attempt after a real drop.
/// </summary>
public class EngineRigTimeoutTests
{
    private static SyncEngine MakeEngine() => new(new AppSettings(), new StubWebViewHost());

    [Fact]
    public async Task GivesUp_OnceTheConnectDeadlineHasPassed()
    {
        var engine = MakeEngine();
        engine._rig = new NeverConnectsRig();
        engine._rigActive = true;
        (engine._rigBackend, engine._rigHost, engine._rigPort) = ("rigctld", "10.0.0.1", 4532);
        // Deadline already in the past -- avoids an actual 10s sleep in the test.
        engine._rigConnectDeadline = MonotonicClock.NowS() - 1.0;

        await engine.TickAsync();

        Assert.False(engine._rigActive);
        Assert.Null(engine._rig);
        Assert.NotNull(engine._rigError);
        Assert.Contains("10.0.0.1:4532", engine._rigError);
        Assert.Contains($"{SyncEngine.RigConnectTimeoutS:F0}s", engine._rigError);
    }

    [Fact]
    public async Task DoesNotGiveUp_BeforeTheDeadline()
    {
        var engine = MakeEngine();
        engine._rig = new NeverConnectsRig();
        engine._rigActive = true;
        (engine._rigBackend, engine._rigHost, engine._rigPort) = ("rigctld", "10.0.0.1", 4532);
        engine._rigConnectDeadline = MonotonicClock.NowS() + 100.0; // far in the future

        await engine.TickAsync();

        Assert.True(engine._rigActive);
        Assert.NotNull(engine._rig);
        Assert.Null(engine._rigError);
    }

    [Fact]
    public async Task SuccessfulConnect_ClearsTheDeadline()
    {
        var engine = MakeEngine();
        var rig = new AlwaysConnectsRig();
        engine._rig = rig;
        engine._rigActive = true;
        engine._rigConnectDeadline = MonotonicClock.NowS() + 100.0;

        await engine.TickAsync();

        Assert.Null(engine._rigConnectDeadline);
        Assert.Equal(1, rig.EnsureConnectedCalls);
    }
}
