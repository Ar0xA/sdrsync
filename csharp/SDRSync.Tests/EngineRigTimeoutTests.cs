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

    [Fact]
    public async Task RigGeneration_LetsGuiDistinguishBacklogFromANewConnectAttempt()
    {
        // Regression test for a live bug: while the rig is idle after a
        // failed connect, TickAsync() keeps publishing a fresh snapshot
        // every poll carrying the same _rigError, so the GUI's status
        // channel can be holding a backlog of stale snapshots from the
        // FAILED attempt at the exact moment the user starts
        // flrig/rigctld and clicks Connect again. Without a generation tag
        // on every snapshot, one of those backlog snapshots could be read
        // right after the click and re-pop "connection failed" for an
        // attempt that hasn't even started yet, even though the new
        // attempt goes on to succeed moments later (confirmed live in the
        // Python original).
        //
        // This pins the engine-side half of the fix: every snapshot from
        // one StartRigAsync() attempt -- including its eventual give-up --
        // shares ONE generation, and the next StartRigAsync() call
        // strictly bumps it before publishing anything else.
        var engine = MakeEngine();

        await engine.StartRigAsync("rigctld", "10.0.0.1", 4532, false);
        var firstGeneration = engine._rigGeneration;
        Assert.Equal(1, firstGeneration);

        // Swap in a stub post-construction -- avoids relying on real
        // networking/timing to reach the give-up branch; StartRigAsync()'s
        // own bookkeeping (generation bump, deadline, publish) already ran
        // for real above, which is the part under test.
        engine._rig = new NeverConnectsRig();
        engine._rigConnectDeadline = MonotonicClock.NowS() - 1.0; // already past
        await engine.TickAsync();
        Assert.False(engine._rigActive);
        Assert.NotNull(engine._rigError);
        // The give-up itself must NOT bump the generation -- it's still
        // reporting the outcome of the SAME attempt the click started.
        Assert.Equal(firstGeneration, engine._rigGeneration);

        // Drain every snapshot StartRigAsync()/TickAsync() published above
        // -- this is exactly the backlog that could still be sitting in
        // the status channel when the user clicks Connect again.
        var backlog = new List<StatusSnapshot>();
        while (engine.StatusReader.TryRead(out var msg))
        {
            if (msg is StatusSnapshot snap) backlog.Add(snap);
        }

        Assert.NotEmpty(backlog);
        Assert.All(backlog, snap => Assert.Equal(firstGeneration, snap.RigGeneration));

        // A fresh Connect click starts a new attempt -- its very first
        // published snapshot must carry a STRICTLY greater generation
        // than every backlog snapshot above, which is what lets the GUI
        // tell them apart regardless of channel timing.
        await engine.StartRigAsync("rigctld", "10.0.0.1", 4532, false);
        Assert.Equal(firstGeneration + 1, engine._rigGeneration);
        Assert.True(engine.StatusReader.TryRead(out var newMsg));
        var newSnap = Assert.IsType<StatusSnapshot>(newMsg);
        Assert.Equal(firstGeneration + 1, newSnap.RigGeneration);
        Assert.True(newSnap.RigGeneration > backlog.Max(s => s.RigGeneration));
    }
}
