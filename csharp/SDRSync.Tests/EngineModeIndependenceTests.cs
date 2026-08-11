using SDRSync.Core;
using SDRSync.Rig;
using SDRSync.Sync;
using SDRSync.WebSdr;
using Xunit;

namespace SDRSync.Tests;

/// <summary>
/// Ported from tests/test_engine_mode_independence.py. An unsupported
/// hamlib mode must not block frequency sync -- the driver is responsible
/// for gracefully skipping modes it can't map; this pins down the
/// engine's half of the contract: keep calling SetModeAsync/TuneHzAsync
/// independently every tick regardless of whether the driver could
/// actually apply the mode. Also covers the v14 "good network citizen"
/// write-rate-limiting round: the global write gap, the per-axis failure
/// ladder, the periodic-resync stamp fix, and the forward-side generation guard.
/// </summary>
public class EngineModeIndependenceTests
{
    private sealed class StubRig(RigState state) : IRigClient
    {
        public RigState State { get; set; } = state;
        public Task<bool> EnsureConnectedAsync() => Task.FromResult(true);
        public Task<RigState> GetStateAsync() => Task.FromResult(State);
        public Task CloseAsync() => Task.CompletedTask;
        public Task<bool> SetFreqAsync(int freqHz, double? verifyBudgetS = null) => Task.FromResult(true);
        public Task<bool> SetModeAsync(string modeName, int? passbandHz, double? verifyBudgetS = null) => Task.FromResult(true);
    }

    /// <summary>attached=true and no-op CloseAsync() so it stands in for a real driver in engine-level tests without a browser.</summary>
    private class StubDriver : IWebSDRDriver
    {
        public bool Attached => true;
        public List<int> Tuned { get; } = new();
        public List<bool> TuneVerifyFlags { get; } = new();
        public List<(string Mode, int? PassbandHz)> Modes { get; } = new();

        public Task AttachAsync(IWebSdrPage page) => Task.CompletedTask;

        public virtual Task<bool> TuneHzAsync(int freqHz, bool verify = true)
        {
            Tuned.Add(freqHz);
            TuneVerifyFlags.Add(verify);
            return Task.FromResult(true);
        }

        public virtual Task<bool> SetModeAsync(string hamlibMode, int? passbandHz)
        {
            Modes.Add((hamlibMode, passbandHz));
            return Task.FromResult(true);
        }

        public Task SetMutedAsync(bool muted) => Task.CompletedTask;

        public Task<WebSDRStatus> GetStatusAsync() => Task.FromResult(new WebSDRStatus(Connected: true));

        public string? HamlibModeFromStatus(WebSDRStatus status) => null;

        public int? RigFreqFromStatus(WebSDRStatus status) => null;

        public Task CloseAsync() => Task.CompletedTask;
    }

    /// <summary>Returns false (no-op/failed) from TuneHzAsync and SetModeAsync -- e.g. what a real driver does while not yet attached, or after a failed page call.</summary>
    private sealed class FailingDriver : StubDriver
    {
        public override Task<bool> TuneHzAsync(int freqHz, bool verify = true)
        {
            Tuned.Add(freqHz);
            return Task.FromResult(false);
        }

        public override Task<bool> SetModeAsync(string hamlibMode, int? passbandHz)
        {
            Modes.Add((hamlibMode, passbandHz));
            return Task.FromResult(false);
        }
    }

    /// <summary>Driver whose push results can be flipped mid-test, for exercising the failure ladder's recovery path.</summary>
    private sealed class TogglingDriver : StubDriver
    {
        public bool ModeResult { get; set; } = true;
        public bool FreqResult { get; set; } = true;

        public override Task<bool> TuneHzAsync(int freqHz, bool verify = true)
        {
            Tuned.Add(freqHz);
            TuneVerifyFlags.Add(verify);
            return Task.FromResult(FreqResult);
        }

        public override Task<bool> SetModeAsync(string hamlibMode, int? passbandHz)
        {
            Modes.Add((hamlibMode, passbandHz));
            return Task.FromResult(ModeResult);
        }
    }

    /// <summary>One sick axis, one healthy one: SetModeAsync always fails, TuneHzAsync always succeeds.</summary>
    private sealed class ModeFailingDriver : StubDriver
    {
        public override Task<bool> SetModeAsync(string hamlibMode, int? passbandHz)
        {
            Modes.Add((hamlibMode, passbandHz));
            return Task.FromResult(false);
        }
    }

    /// <summary>SetModeAsync suspends for real, giving another task on the same loop a chance to run mid-await.</summary>
    private sealed class SlowModeDriver : StubDriver
    {
        public TaskCompletionSource EnteredSetMode { get; private set; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public void ResetEnteredSignal() => EnteredSetMode = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);

        public override async Task<bool> SetModeAsync(string hamlibMode, int? passbandHz)
        {
            Modes.Add((hamlibMode, passbandHz));
            EnteredSetMode.TrySetResult();
            await Task.Delay(20);
            return true;
        }
    }

    private sealed class SlowTuneDriver : StubDriver
    {
        public TaskCompletionSource EnteredTune { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public override async Task<bool> TuneHzAsync(int freqHz, bool verify = true)
        {
            Tuned.Add(freqHz);
            TuneVerifyFlags.Add(verify);
            EnteredTune.TrySetResult();
            await Task.Delay(20);
            return true;
        }
    }

    private static SyncEngine MakeEngine() => new(new AppSettings(), new StubWebViewHost());

    /// <summary>Backdates the global write-rate limiter so consecutive ticks with no real wall-clock delay aren't throttled by it.</summary>
    private static void ClearWebsdrWriteGap(SyncEngine engine) => engine._lastWebsdrWriteAt -= SyncEngine.WebsdrMinWriteGapS + 0.1;

    /// <summary>Backdates both per-axis forward-push failure ladders so a test exercising repeated failing pushes isn't waiting out the real backoff base.</summary>
    private static void ClearForwardBackoff(SyncEngine engine)
    {
        engine._forwardFreqBackoff.NextAttemptAt = 0.0;
        engine._forwardModeBackoff.NextAttemptAt = 0.0;
    }

    [Fact]
    public async Task UnsupportedMode_DoesNotBlockFrequencySync()
    {
        var engine = MakeEngine();
        var stubRig = new StubRig(new RigState(14074000, "DSTAR", 2700, false));
        var stubDriver = new StubDriver();
        engine._rig = stubRig;
        engine._rigActive = true;
        engine._driver = stubDriver;
        engine._websdrActive = true;

        await engine.TickAsync();
        engine._pendingFreqSince -= 1.0;
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync();

        // The engine calls SetModeAsync every tick the (mode, passband)
        // changes, regardless of whether the driver could map "DSTAR" --
        // that's the driver's job to skip gracefully, not the engine's.
        Assert.Equal(new[] { ("DSTAR", (int?)2700) }, stubDriver.Modes);
        Assert.Equal(new[] { 14074000 }, stubDriver.Tuned);
    }

    [Fact]
    public async Task FailedPush_IsRetriedNotLatchedAsSent()
    {
        var engine = MakeEngine();
        var stubRig = new StubRig(new RigState(14074000, "USB", 2700, false));
        var failingDriver = new FailingDriver();
        engine._rig = stubRig;
        engine._rigActive = true;
        engine._driver = failingDriver;
        engine._websdrActive = true;

        for (var i = 0; i < 4; i++)
        {
            await engine.TickAsync();
            engine._pendingFreqSince -= 1.0;
            ClearWebsdrWriteGap(engine);
            ClearForwardBackoff(engine);
        }

        Assert.Null(engine._lastSentFreq);
        Assert.Null(engine._lastSentModeKey);
        Assert.True(failingDriver.Tuned.Count(f => f == 14074000) >= 2);
        Assert.True(failingDriver.Modes.Count(m => m == ("USB", 2700)) >= 2);
    }

    [Fact]
    public async Task PeriodicFullResync_RepushesEvenWhenDedupeLatchesAlreadyMatch()
    {
        var engine = MakeEngine();
        var stubRig = new StubRig(new RigState(14074000, "USB", 2700, false));
        var stubDriver = new StubDriver();
        engine._rig = stubRig;
        engine._rigActive = true;
        engine._driver = stubDriver;
        engine._websdrActive = true;

        await engine.TickAsync();
        engine._pendingFreqSince -= 1.0;
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync();
        Assert.Equal(new[] { ("USB", (int?)2700) }, stubDriver.Modes);
        Assert.Equal(new[] { 14074000 }, stubDriver.Tuned);

        // Nothing changed on the rig -- a normal tick must NOT re-push.
        await engine.TickAsync();
        Assert.Equal(new[] { ("USB", (int?)2700) }, stubDriver.Modes);
        Assert.Equal(new[] { 14074000 }, stubDriver.Tuned);

        // Once the periodic resync interval has elapsed, the next tick
        // re-pushes both, even though the dedupe latches still match.
        engine._lastFullResyncAt -= SyncEngine.FullResyncIntervalS + 0.1;
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync();
        Assert.Equal(new[] { ("USB", (int?)2700), ("USB", (int?)2700) }, stubDriver.Modes);
        Assert.Equal(new[] { 14074000, 14074000 }, stubDriver.Tuned);
    }

    [Fact]
    public async Task PeriodicResyncOfUnchangedFreq_StillVerifiesWithNoReversePushInFlight()
    {
        var engine = MakeEngine();
        var stubRig = new StubRig(new RigState(14074000, "USB", 2700, false));
        var stubDriver = new StubDriver();
        engine._rig = stubRig;
        engine._rigActive = true;
        engine._driver = stubDriver;
        engine._websdrActive = true;

        await engine.TickAsync();
        engine._pendingFreqSince -= 1.0;
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync();
        Assert.Equal(new[] { 14074000 }, stubDriver.Tuned);
        Assert.Equal(new[] { true }, stubDriver.TuneVerifyFlags); // a genuine change always verifies

        // Drop the rig's mode for this tick so the mode branch is skipped
        // and _lastSentFreq survives -- now freqChanged is genuinely false
        // and only the refinement can make verify true.
        stubRig.State = new RigState(14074000, null, null, false);
        engine._lastFullResyncAt -= SyncEngine.FullResyncIntervalS + 0.1;
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync();
        Assert.Equal(new[] { 14074000, 14074000 }, stubDriver.Tuned);
        Assert.Equal(new[] { true, true }, stubDriver.TuneVerifyFlags);
    }

    [Fact]
    public async Task PeriodicResyncOfUnchangedFreq_SkipsVerifyWhileReversePushInFlight()
    {
        // The one case TuneHzAsync(verify: false) is actually for -- a
        // reverse-sync mode push is actively retrying when the periodic
        // resync of an UNCHANGED frequency fires.
        var engine = MakeEngine();
        var stubRig = new StubRig(new RigState(14074000, "USB", 2700, false));
        var stubDriver = new StubDriver();
        engine._rig = stubRig;
        engine._rigActive = true;
        engine._driver = stubDriver;
        engine._websdrActive = true;

        await engine.TickAsync();
        engine._pendingFreqSince -= 1.0;
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync();
        Assert.Equal(new[] { true }, stubDriver.TuneVerifyFlags);

        engine._lastFullResyncAt -= SyncEngine.FullResyncIntervalS + 0.1;
        engine._modePush = new ReversePush("LSB"); // a reverse-sync mode ladder is actively retrying
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync();
        Assert.Equal(new[] { 14074000, 14074000 }, stubDriver.Tuned);
        Assert.Equal(new[] { true, false }, stubDriver.TuneVerifyFlags);
    }

    [Fact]
    public async Task WriteGap_LetsOnlyOneOfTwoBackToBackWritesReachTheDriver()
    {
        var engine = MakeEngine();
        engine._rig = new StubRig(new RigState(14074000, "USB", 2700, false));
        engine._rigActive = true;
        var failingDriver = new FailingDriver();
        engine._driver = failingDriver;
        engine._websdrActive = true;

        await engine.TickAsync(); // mode pushes (freq only arms its debounce)
        Assert.Equal(new[] { ("USB", (int?)2700) }, failingDriver.Modes);
        Assert.Empty(failingDriver.Tuned);

        // Second tick, no wall-clock delay: the freq push is otherwise
        // fully eligible but must be held off.
        engine._pendingFreqSince -= 1.0;
        ClearForwardBackoff(engine);
        await engine.TickAsync();
        Assert.Empty(failingDriver.Tuned);

        // Once the gap has elapsed it goes through.
        ClearWebsdrWriteGap(engine);
        ClearForwardBackoff(engine);
        await engine.TickAsync();
        Assert.Equal(new[] { 14074000 }, failingDriver.Tuned);
    }

    [Fact]
    public async Task WriteGap_IsOneGlobalFloorSharedByBothAxes()
    {
        var engine = MakeEngine();
        engine._rig = new StubRig(new RigState(14074000, "USB", 2700, false));
        engine._rigActive = true;
        var stubDriver = new StubDriver();
        engine._driver = stubDriver;
        engine._websdrActive = true;

        await engine.TickAsync(); // mode pushes, freq arms its debounce
        engine._pendingFreqSince -= 1.0;
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync(); // freq pushes
        Assert.Equal(new[] { ("USB", (int?)2700) }, stubDriver.Modes);
        Assert.Equal(new[] { 14074000 }, stubDriver.Tuned);

        // A resync forces BOTH axes in one tick: both go through on the
        // one shared gate.
        engine._lastFullResyncAt -= SyncEngine.FullResyncIntervalS + 0.1;
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync();
        Assert.Equal(new[] { ("USB", (int?)2700), ("USB", (int?)2700) }, stubDriver.Modes);
        Assert.Equal(new[] { 14074000, 14074000 }, stubDriver.Tuned);
    }

    [Fact]
    public void ForwardPushBackoffLadder_DoublesAndCaps()
    {
        var backoff = new ForwardPushBackoff();
        foreach (var expected in new[] { 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0 })
        {
            var before = MonotonicClock.NowS();
            backoff.RecordFailure(MonotonicClock.NowS());
            Assert.True(Math.Abs(backoff.NextAttemptAt - before - expected) <= 0.05);
        }

        Assert.Equal(7, backoff.Failures);
    }

    [Fact]
    public void ForwardPushBackoff_SuccessResetsTheLadder()
    {
        var backoff = new ForwardPushBackoff();
        backoff.RecordFailure(MonotonicClock.NowS());
        backoff.RecordFailure(MonotonicClock.NowS());
        Assert.Equal(2, backoff.Failures);
        backoff.RecordSuccess();
        Assert.Equal(0, backoff.Failures);
        Assert.Equal(0.0, backoff.NextAttemptAt);
    }

    [Fact]
    public void ForwardPushBackoff_NewTargetGrantsOneImmediateRetryWhileShallow()
    {
        var backoff = new ForwardPushBackoff();
        backoff.NoteTarget(14_074_000);
        backoff.RecordFailure(MonotonicClock.NowS());
        Assert.True(backoff.NextAttemptAt > MonotonicClock.NowS()); // backing off

        backoff.NoteTarget(14_200_000); // the rig genuinely moved
        Assert.Equal(0.0, backoff.NextAttemptAt); // one prompt try for the new value
        Assert.Equal(1, backoff.Failures); // ...but the failure count itself is NOT forgiven
    }

    [Fact]
    public void ForwardPushBackoff_NewTargetDoesNotEvadeADeepLadder()
    {
        var backoff = new ForwardPushBackoff();
        for (var i = 0; i < SyncEngine.ForwardPushImmediateRetryMaxFailures; i++)
        {
            backoff.RecordFailure(MonotonicClock.NowS());
        }

        Assert.Equal(SyncEngine.ForwardPushImmediateRetryMaxFailures, backoff.Failures);
        var armedAt = backoff.NextAttemptAt;

        backoff.NoteTarget("a completely new target");
        Assert.Equal(armedAt, backoff.NextAttemptAt); // waits out the backoff like anything else
    }

    [Fact]
    public async Task FailingForwardPush_BacksOffInsteadOfRetryingEveryTick()
    {
        var engine = MakeEngine();
        engine._rig = new StubRig(new RigState(14074000, "USB", 2700, false));
        engine._rigActive = true;
        var failingDriver = new FailingDriver();
        engine._driver = failingDriver;
        engine._websdrActive = true;

        await engine.TickAsync(); // mode push fails -> ladder arms
        Assert.Equal(new[] { ("USB", (int?)2700) }, failingDriver.Modes);
        Assert.Equal(1, engine._forwardModeBackoff.Failures);

        ClearWebsdrWriteGap(engine);
        await engine.TickAsync();
        Assert.Equal(new[] { ("USB", (int?)2700) }, failingDriver.Modes); // not retried yet

        ClearForwardBackoff(engine);
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync();
        Assert.Equal(new[] { ("USB", (int?)2700), ("USB", (int?)2700) }, failingDriver.Modes);
        Assert.Equal(2, engine._forwardModeBackoff.Failures);
    }

    [Fact]
    public async Task SuccessfulPush_ClearsAPreviouslyArmedForwardLadder()
    {
        var engine = MakeEngine();
        engine._rig = new StubRig(new RigState(14074000, "USB", 2700, false));
        engine._rigActive = true;
        var driver = new TogglingDriver { ModeResult = false };
        engine._driver = driver;
        engine._websdrActive = true;

        await engine.TickAsync();
        Assert.Equal(1, engine._forwardModeBackoff.Failures);

        driver.ModeResult = true; // the WebSDR recovers
        ClearForwardBackoff(engine);
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync();
        Assert.Equal(0, engine._forwardModeBackoff.Failures);
        Assert.Equal(0.0, engine._forwardModeBackoff.NextAttemptAt);
    }

    [Fact]
    public async Task PeriodicResyncHeldBackByTheWriteGap_IsNotStampedAsDone()
    {
        var engine = MakeEngine();
        engine._rig = new StubRig(new RigState(14074000, "USB", 2700, false));
        engine._rigActive = true;
        var stubDriver = new StubDriver();
        engine._driver = stubDriver;
        engine._websdrActive = true;

        await engine.TickAsync();
        engine._pendingFreqSince -= 1.0;
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync();
        Assert.Equal(new[] { ("USB", (int?)2700) }, stubDriver.Modes);
        Assert.Equal(new[] { 14074000 }, stubDriver.Tuned);

        // A resync comes due while the write gap is still closed.
        engine._lastFullResyncAt -= SyncEngine.FullResyncIntervalS + 0.1;
        var stampBefore = engine._lastFullResyncAt;
        await engine.TickAsync();

        Assert.Equal(new[] { ("USB", (int?)2700) }, stubDriver.Modes); // nothing was sent...
        Assert.Equal(new[] { 14074000 }, stubDriver.Tuned);
        Assert.Equal(stampBefore, engine._lastFullResyncAt); // ...so nothing recorded as done

        // The very next opportunity still attempts it.
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync();
        Assert.Equal(new[] { ("USB", (int?)2700), ("USB", (int?)2700) }, stubDriver.Modes);
        Assert.Equal(new[] { 14074000, 14074000 }, stubDriver.Tuned);
        Assert.True(engine._lastFullResyncAt > stampBefore);
    }

    [Fact]
    public async Task ALadderBlockedAxis_StillBlocksItsOwnRepush()
    {
        var engine = MakeEngine();
        engine._rig = new StubRig(new RigState(14074000, "USB", 2700, false));
        engine._rigActive = true;
        var failingDriver = new FailingDriver();
        engine._driver = failingDriver;
        engine._websdrActive = true;

        await engine.TickAsync(); // mode push fails, arming the mode ladder
        Assert.Equal(1, engine._forwardModeBackoff.Failures);

        engine._lastFullResyncAt -= SyncEngine.FullResyncIntervalS + 0.1;
        ClearWebsdrWriteGap(engine); // only the ladder is in the way now
        await engine.TickAsync();

        Assert.Equal(new[] { ("USB", (int?)2700) }, failingDriver.Modes); // ladder blocked the re-push
    }

    [Fact]
    public async Task OneAxisInItsFailureLadder_DoesNotHoldTheResyncStampHostage()
    {
        var engine = MakeEngine();
        engine._rig = new StubRig(new RigState(14074000, "USB", 2700, false));
        engine._rigActive = true;
        var driver = new ModeFailingDriver();
        engine._driver = driver;
        engine._websdrActive = true;

        await engine.TickAsync(); // mode push fails -> mode ladder armed; freq debounce arms
        Assert.Equal(1, engine._forwardModeBackoff.Failures);
        Assert.Empty(driver.Tuned);

        engine._lastFullResyncAt -= SyncEngine.FullResyncIntervalS + 0.1;
        var stampBefore = engine._lastFullResyncAt;
        engine._pendingFreqSince -= 1.0;
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync();

        Assert.Equal(new[] { ("USB", (int?)2700) }, driver.Modes); // the sick axis correctly stayed quiet...
        Assert.Equal(new[] { 14074000 }, driver.Tuned); // ...the healthy one did its resync push...
        Assert.True(engine._lastFullResyncAt > stampBefore); // ...and that counted as done

        engine._pendingFreqSince -= 1.0;
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync();
        Assert.Equal(new[] { 14074000 }, driver.Tuned);
    }

    [Fact]
    public async Task ResetDuringForwardModePush_DiscardsTheStaleWrite()
    {
        var engine = MakeEngine();
        engine._rig = new StubRig(new RigState(14074000, "USB", 2700, false));
        engine._rigActive = true;
        var driver = new SlowModeDriver();
        engine._driver = driver;
        engine._websdrActive = true;

        var tick = engine.TickAsync();
        await driver.EnteredSetMode.Task;
        engine.ResetSyncLatches(); // the attach supervisor, on this same loop
        await tick;

        Assert.Equal(new[] { ("USB", (int?)2700) }, driver.Modes); // the write did physically happen
        Assert.Null(engine._lastSentModeKey);
        Assert.Null(engine._lastPushedToWebsdrMode);
        Assert.False(engine._reverseReseedDue);
    }

    [Fact]
    public async Task ResetDuringForwardFreqPush_DiscardsTheStaleWrite()
    {
        var engine = MakeEngine();
        engine._rig = new StubRig(new RigState(14074000, null, null, false));
        engine._rigActive = true;
        var driver = new SlowTuneDriver();
        engine._driver = driver;
        engine._websdrActive = true;

        await engine.TickAsync(); // arms the freq debounce (mode is None: nothing to push)
        engine._pendingFreqSince -= 1.0;
        ClearWebsdrWriteGap(engine);

        var tick = engine.TickAsync();
        await driver.EnteredTune.Task;
        engine.ResetSyncLatches();
        await tick;

        Assert.Equal(new[] { 14074000 }, driver.Tuned);
        Assert.Null(engine._lastSentFreq);
        Assert.Null(engine._lastPushedToWebsdrFreq);
        Assert.False(engine._reverseReseedDue);
    }

    [Fact]
    public async Task HoldToggleDuringAForwardPush_DoesNotDiscardItsBookkeeping()
    {
        var engine = MakeEngine();
        engine._rig = new StubRig(new RigState(14074000, "USB", 2700, false));
        engine._rigActive = true;
        var driver = new SlowModeDriver();
        engine._driver = driver;
        engine._websdrActive = true;

        var tick = engine.TickAsync();
        await driver.EnteredSetMode.Task;
        engine.ApplyReverseSyncHeld(true); // the GUI thread, marshalled onto this loop
        await tick;

        Assert.Equal(new[] { ("USB", (int?)2700) }, driver.Modes);
        // Hold is a reverse-side concept: the forward push that was
        // already in flight still latches normally.
        Assert.Equal(("USB", (int?)2700), engine._lastSentModeKey);
        Assert.Equal("USB", engine._lastPushedToWebsdrMode);
        Assert.True(engine._reverseSyncHeld);
    }

    [Fact]
    public void HoldToggle_StillSupersedesReverseSyncState()
    {
        var engine = MakeEngine();
        var before = engine._syncLatchGeneration;
        engine.ApplyReverseSyncHeld(true);
        Assert.True(engine._syncLatchGeneration > before);
    }

    [Fact]
    public void Reset_BumpsBothGenerationCounters()
    {
        var engine = MakeEngine();
        var forwardBefore = engine._forwardLatchGeneration;
        var reverseBefore = engine._syncLatchGeneration;
        engine.ResetSyncLatches();
        Assert.True(engine._forwardLatchGeneration > forwardBefore);
        Assert.True(engine._syncLatchGeneration > reverseBefore);
    }

    [Fact]
    public async Task SupersededModePush_SkipsTheRestOfTheForwardBlock()
    {
        var engine = MakeEngine();
        var stubRig = new StubRig(new RigState(14074000, "USB", 2700, false));
        engine._rig = stubRig;
        engine._rigActive = true;
        var driver = new SlowModeDriver();
        engine._driver = driver;
        engine._websdrActive = true;

        await engine.TickAsync(); // arms the freq debounce and latches the mode
        stubRig.State = new RigState(14074000, "CW", 500, false);
        engine._pendingFreqSince -= 1.0;
        ClearWebsdrWriteGap(engine);

        driver.ResetEnteredSignal();
        var tick = engine.TickAsync();
        await driver.EnteredSetMode.Task;
        var pending = engine._pendingFreq;
        engine.ResetSyncLatches();
        engine._pendingFreq = pending; // see the Python original's docstring for why
        engine._pendingFreqSince = MonotonicClock.NowS() - 1.0;
        await tick;

        Assert.Equal(new[] { ("USB", (int?)2700), ("CW", (int?)500) }, driver.Modes);
        // The frequency axis belonged to the same superseded cycle, so it
        // must not have gone out to the (now replaced) page at all.
        Assert.Empty(driver.Tuned);
    }

    [Fact]
    public void ForwardPushBackoffExponent_IsClamped()
    {
        // Not merely wasteful: at very high failure counts, an unclamped
        // exponent would overflow computing 2^(failures-1) as a double.
        var backoff = new ForwardPushBackoff();
        foreach (var failures in new[] { 1024, 5000 })
        {
            backoff.Failures = failures - 1;
            backoff.RecordFailure(MonotonicClock.NowS()); // must not throw
            Assert.True(backoff.NextAttemptAt - MonotonicClock.NowS() <= SyncEngine.ForwardPushBackoffMaxS + 0.5);
        }
    }

    [Fact]
    public async Task ForwardSyncPaused_BlocksBothAxesAndUnpausingFiresThePendingPush()
    {
        var engine = MakeEngine();
        var stubRig = new StubRig(new RigState(14074000, "USB", 2700, false));
        var stubDriver = new StubDriver();
        engine._rig = stubRig;
        engine._rigActive = true;
        engine._driver = stubDriver;
        engine._websdrActive = true;
        engine._forwardSyncPaused = true;

        await engine.TickAsync(); // baseline tick, nothing pending yet
        stubRig.State = new RigState(14075000, "CW", 500, false);
        engine._pendingFreqSince -= 1.0;
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync(); // still paused -- must be a complete no-op push-wise
        Assert.Empty(stubDriver.Tuned);
        Assert.Empty(stubDriver.Modes);

        engine._forwardSyncPaused = false;
        engine._pendingFreqSince -= 1.0;
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync(); // unpaused -- the pending target must go out now

        Assert.Equal(new[] { 14075000 }, stubDriver.Tuned);
        Assert.Equal(new[] { ("CW", (int?)500) }, stubDriver.Modes);
    }

    [Fact]
    public void SetForwardSyncPausedFromOtherThread_SetsDirectlyBeforeLoopStarts()
    {
        // Mirrors SetReverseSyncHeld's own before-loop-starts contract:
        // with the engine loop not yet started, the setter must write the
        // field directly rather than silently drop the call.
        var engine = MakeEngine();
        Assert.False(engine._started);
        engine.SetForwardSyncPausedFromOtherThread(true);
        Assert.True(engine._forwardSyncPaused);
        engine.SetForwardSyncPausedFromOtherThread(false);
        Assert.False(engine._forwardSyncPaused);
    }
}
