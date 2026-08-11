using SDRSync.Core;
using SDRSync.Rig;
using SDRSync.Sync;
using SDRSync.WebSdr;
using Xunit;

namespace SDRSync.Tests;

/// <summary>
/// Ported from tests/test_engine_reverse_sync.py. Covers v11 reverse sync
/// (WebSDR -> rig) dedupe/loop-prevention: an echo of the engine's own
/// just-pushed value must NOT trigger a reverse push; a genuinely new
/// value must trigger exactly one SetFreqAsync/SetModeAsync call,
/// correctly debounced; a value observed while transmitting must be fully
/// suppressed; the first observation after attach is a baseline (not
/// pushed); the retry ladder, range guard, CW-variant echo, Hold toggle,
/// and generation-counter races against concurrent resets.
///
/// Note on sequencing (from the Python original): the engine's own first
/// tick after attach always forward-pushes the rig's initial mode (freq
/// arms but doesn't push until its own debounce elapses), and a
/// successful forward push resets the ReverseHoldoffS window from inside
/// that same tick, so reverse sync never runs on that first tick.
/// SettleAndCaptureBaselineAsync drives enough ticks to reach a tick where
/// nothing new needs forward-pushing, which is when the reverse baseline
/// actually gets captured -- this mirrors real engine behavior, not a
/// test shortcut.
/// </summary>
public class EngineReverseSyncTests
{
    private sealed class StubReverseRig(RigState state) : IRigClient
    {
        public RigState State { get; set; } = state;
        public List<int> SetFreqs { get; } = new();
        public List<(string Mode, int? PassbandHz)> SetModes { get; } = new();
        public bool SetFreqResult { get; set; } = true;
        public bool SetModeResult { get; set; } = true;

        /// <summary>
        /// Stands in for the Python tests' instance-method monkeypatch
        /// (`stub_rig.set_freq = ...`) used to inject a concurrent reset/Hold
        /// toggle mid-await -- C# can't reassign a method on an instance,
        /// so the stub checks for an override delegate first.
        /// </summary>
        public Func<int, double?, Task<bool>>? SetFreqOverride { get; set; }

        public Func<string, int?, double?, Task<bool>>? SetModeOverride { get; set; }

        public Task<bool> EnsureConnectedAsync() => Task.FromResult(true);
        public Task<RigState> GetStateAsync() => Task.FromResult(State);
        public Task CloseAsync() => Task.CompletedTask;

        public Task<bool> SetFreqAsync(int freqHz, double? verifyBudgetS = null)
        {
            if (SetFreqOverride is not null) return SetFreqOverride(freqHz, verifyBudgetS);
            SetFreqs.Add(freqHz);
            return Task.FromResult(SetFreqResult);
        }

        public Task<bool> SetModeAsync(string modeName, int? passbandHz, double? verifyBudgetS = null)
        {
            if (SetModeOverride is not null) return SetModeOverride(modeName, passbandHz, verifyBudgetS);
            SetModes.Add((modeName, passbandHz));
            return Task.FromResult(SetModeResult);
        }
    }

    /// <summary>attached=true, and HamlibModeFromStatus/RigFreqFromStatus that just pass the status's own fields through -- these tests are about engine-level dedupe/debounce, not per-driver mode-mapping/CW-offset.</summary>
    private class StubReverseDriver(WebSDRStatus status) : IWebSDRDriver
    {
        public bool Attached => true;
        public WebSDRStatus Status { get; set; } = status;
        public List<int> Tuned { get; } = new();
        public List<(string Mode, int? PassbandHz)> Modes { get; } = new();

        public Task AttachAsync(IWebSdrPage page) => Task.CompletedTask;

        public virtual Task<bool> TuneHzAsync(int freqHz, bool verify = true)
        {
            Tuned.Add(freqHz);
            return Task.FromResult(true);
        }

        public virtual Task<bool> SetModeAsync(string hamlibMode, int? passbandHz)
        {
            Modes.Add((hamlibMode, passbandHz));
            return Task.FromResult(true);
        }

        public Task SetMutedAsync(bool muted) => Task.CompletedTask;

        public Task<WebSDRStatus> GetStatusAsync() => Task.FromResult(Status);

        public virtual string? HamlibModeFromStatus(WebSDRStatus status) => status.Mode;

        public virtual int? RigFreqFromStatus(WebSDRStatus status) => status.FreqHz;

        public Task CloseAsync() => Task.CompletedTask;
    }

    /// <summary>A driver whose forward TuneHzAsync/SetModeAsync always fail, e.g. the real websdr_org behavior when the rig's frequency is outside every band the WebSDR covers.</summary>
    private sealed class OutOfBandDriver(WebSDRStatus status) : StubReverseDriver(status)
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

    private sealed class UnmappedModeDriver(WebSDRStatus status) : StubReverseDriver(status)
    {
        public override string? HamlibModeFromStatus(WebSDRStatus status) => status.Mode == "IQ" ? null : status.Mode;
    }

    private const int CwOffsetHz = 700;

    /// <summary>
    /// RigFreqFromStatus applies a non-trivial offset, the way a real
    /// driver's CW-offset conversion does, so the page frequency and the
    /// rig-native frequency are distinguishable values -- StubReverseDriver's
    /// identity passthrough can't tell those apart.
    /// </summary>
    private sealed class CwOffsetReverseDriver(WebSDRStatus status) : StubReverseDriver(status)
    {
        public override int? RigFreqFromStatus(WebSDRStatus status) => status.FreqHz is null ? null : status.FreqHz - CwOffsetHz;
    }

    private static SyncEngine MakeEngine() => new(new AppSettings(), new StubWebViewHost());

    private static void ClearHoldoff(SyncEngine engine) => engine._forwardPushCompletedAt -= SyncEngine.ReverseHoldoffS + 0.1;
    private static void ClearReverseDebounce(SyncEngine engine) => engine._pendingReverseFreqSince -= SyncEngine.ReverseFreqDebounceS + 0.1;
    private static void ClearReverseModeDebounce(SyncEngine engine) => engine._pendingReverseModeSince -= SyncEngine.ReverseModeDebounceS + 0.1;

    private static void ClearPushBackoff(SyncEngine engine)
    {
        if (engine._modePush is not null) engine._modePush.NextAttemptAt = 0.0;
        if (engine._freqPush is not null) engine._freqPush.NextAttemptAt = 0.0;
    }

    private static void ClearRigWriteGap(SyncEngine engine) => engine._lastRigWriteAt -= SyncEngine.ReverseMinRigWriteGapS + 0.1;
    private static void ClearWebsdrWriteGap(SyncEngine engine) => engine._lastWebsdrWriteAt -= SyncEngine.WebsdrMinWriteGapS + 0.1;

    private static async Task SettleAndCaptureBaselineAsync(SyncEngine engine)
    {
        ClearHoldoff(engine);
        await engine.TickAsync(); // mode forward-pushes; freq only arms
        engine._pendingFreqSince -= 1.0;
        ClearWebsdrWriteGap(engine);
        ClearHoldoff(engine);
        await engine.TickAsync(); // freq forward-pushes now
        ClearHoldoff(engine);
        await engine.TickAsync(); // nothing left to forward-push -> baseline captured
        Assert.True(engine._reverseBaselineCaptured);
    }

    [Fact]
    public async Task FirstObservation_IsABaselineNotAPush()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);

        await SettleAndCaptureBaselineAsync(engine);

        Assert.Empty(stubRig.SetFreqs);
        Assert.Empty(stubRig.SetModes);
    }

    [Fact]
    public async Task GenuinelyNewFreq_TriggersExactlyOneDebouncedPush()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.FreqHz = 14200000;
        ClearHoldoff(engine);
        await engine.TickAsync(); // arms the reverse debounce, no push yet
        Assert.Empty(stubRig.SetFreqs);
        ClearReverseDebounce(engine);
        ClearHoldoff(engine);

        await engine.TickAsync(); // debounce elapsed -> pushes once
        Assert.Equal(new[] { 14200000 }, stubRig.SetFreqs);

        ClearHoldoff(engine);
        await engine.TickAsync();
        Assert.Equal(new[] { 14200000 }, stubRig.SetFreqs);
    }

    [Fact]
    public async Task EchoOfOwnPushedValue_DoesNotTriggerAReversePush()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);

        // Deliberately do NOT clear the hold-off here.
        await engine.TickAsync(); // mode forward-pushes
        engine._pendingFreqSince -= 1.0;
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync(); // freq forward-pushes
        Assert.Equal(new[] { ("USB", (int?)2700) }, stubDriver.Modes);
        Assert.Equal(new[] { 14074000 }, stubDriver.Tuned);

        await engine.TickAsync(); // still within ReverseHoldoffS
        Assert.Empty(stubRig.SetFreqs);
        Assert.Empty(stubRig.SetModes);
    }

    [Fact]
    public async Task Transmitting_FullySuppressesReversePush()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        stubRig.State = stubRig.State with { Ptt = true };
        status.FreqHz = 14200000;
        ClearHoldoff(engine);
        await engine.TickAsync();
        ClearReverseDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync();

        Assert.Empty(stubRig.SetFreqs);
    }

    [Fact]
    public async Task GenuinelyNewMode_TriggersExactlyOnePush()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.Mode = "LSB";
        ClearHoldoff(engine);
        await engine.TickAsync(); // arms the reverse mode debounce, no push yet
        Assert.Empty(stubRig.SetModes);
        ClearReverseModeDebounce(engine);
        ClearHoldoff(engine);

        await engine.TickAsync(); // debounce elapsed -> pushes once
        Assert.Equal(new[] { ("LSB", (int?)null) }, stubRig.SetModes);

        ClearHoldoff(engine);
        await engine.TickAsync();
        Assert.Equal(new[] { ("LSB", (int?)null) }, stubRig.SetModes);
    }

    [Fact]
    public async Task ReverseModePush_ReusesRigPassbandWhenBaseModeUnchanged()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 1800, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._driver) = (stubRig, stubDriver);
        engine._reverseBaselineCaptured = true;
        engine._lastObservedModeKey = "LSB";
        engine._lastPushedToWebsdrMode = "LSB";
        engine._pendingReverseMode = "USB";
        engine._pendingReverseModeSince = 0.0;

        await engine.ReverseSyncTickAsync(status, stubRig.State);

        Assert.Equal(new[] { ("USB", (int?)1800) }, stubRig.SetModes);
    }

    [Fact]
    public async Task BaselineFreq_PreventsYankToWebsdrDefaultAfterForwardFailure()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(145500000, "FM", 15000, false));
        // WebSDR's own default -- the rig (2m FM) can never reach this via
        // a forward push, so it just sits there, unrelated to anything the user did.
        var status = new WebSDRStatus(true, 7100000, "USB");
        var stubDriver = new OutOfBandDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);

        for (var i = 0; i < 5; i++)
        {
            ClearHoldoff(engine);
            await engine.TickAsync();
        }

        Assert.Empty(stubRig.SetFreqs);
        Assert.Empty(stubRig.SetModes);
    }

    [Fact]
    public async Task RejectedFreqReversePush_GivesUpAfterMaxAttemptsAndRevertsToRig()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false)) { SetFreqResult = false };
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.FreqHz = 30000000; // rejected by the rig every time it's tried
        ClearHoldoff(engine);
        await engine.TickAsync(); // arms the reverse debounce, no push yet
        Assert.Empty(stubRig.SetFreqs);

        for (var i = 0; i < SyncEngine.ReversePushMaxAttempts; i++)
        {
            ClearReverseDebounce(engine);
            ClearPushBackoff(engine);
            ClearRigWriteGap(engine);
            ClearHoldoff(engine);
            await engine.TickAsync();
        }

        Assert.Equal(SyncEngine.ReversePushMaxAttempts, stubRig.SetFreqs.Count(f => f == 30000000));
        Assert.Null(engine._freqPush); // ladder gave up, not left dangling
        Assert.NotNull(engine._reverseSyncError);
        Assert.Null(engine._lastSentFreq); // forces forward re-assert of the rig's real freq

        ClearHoldoff(engine);
        await engine.TickAsync();
        Assert.Equal(SyncEngine.ReversePushMaxAttempts, stubRig.SetFreqs.Count(f => f == 30000000));
    }

    [Fact]
    public async Task ForwardPushEchoWithCollapsedMode_DoesNotBounceBack()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        // Rig switches to a mode that forward-collapses to the SAME WebSDR
        // string ("USB") -- the page would still read "USB", but the
        // rig's own reported mode is now "PKTUSB".
        stubRig.State = stubRig.State with { Mode = "PKTUSB" };
        ClearHoldoff(engine);
        ClearWebsdrWriteGap(engine);
        await engine.TickAsync(); // forward push applies PKTUSB; page stays "USB"
        Assert.Equal(("PKTUSB", (int?)2700), stubDriver.Modes[^1]);

        ClearHoldoff(engine);
        await engine.TickAsync(); // reseed tick: page still "USB" -> nothing to push
        ClearHoldoff(engine);
        await engine.TickAsync(); // fully settled -> still nothing to push

        Assert.Empty(stubRig.SetModes);
    }

    [Fact]
    public async Task PttNone_InheritsLastKnownStateForReverseGate()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, true));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        ClearHoldoff(engine);
        await engine.TickAsync(); // establishes _lastPtt = true (transmitting)

        stubRig.State = stubRig.State with { Ptt = null }; // transient read failure, still actually mid-TX
        status.FreqHz = 14200000;
        ClearHoldoff(engine);
        await engine.TickAsync();
        ClearReverseDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync();

        Assert.Empty(stubRig.SetFreqs);
    }

    [Fact]
    public async Task UnmappedReverseMode_LogsAndStillReverseSyncsFrequency()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new UnmappedModeDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.Mode = "IQ";
        status.FreqHz = 14200000;
        ClearHoldoff(engine);
        await engine.TickAsync(); // mode unmapped -> no mode push; freq arms
        Assert.Empty(stubRig.SetModes);
        ClearReverseDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync();

        Assert.Equal(new[] { 14200000 }, stubRig.SetFreqs);
    }

    [Fact]
    public async Task ConcurrentResetDuringAwait_WinsOverStaleReversePush()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.FreqHz = 14200000;
        ClearHoldoff(engine);
        await engine.TickAsync(); // arms the reverse debounce
        ClearReverseDebounce(engine);
        ClearHoldoff(engine);

        stubRig.SetFreqOverride = (freqHz, _) =>
        {
            // Simulates the attach supervisor's ResetSyncLatches() landing
            // while this call is in flight.
            engine.ResetSyncLatches();
            stubRig.SetFreqs.Add(freqHz);
            return Task.FromResult(true);
        };
        await engine.TickAsync();

        Assert.False(engine._reverseBaselineCaptured);
    }

    [Fact]
    public async Task ModeLadder_RetriesAndSucceedsBeforeGivingUp()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        stubRig.SetModeResult = false; // first attempt "fails" (unconfirmed)
        status.Mode = "LSB";
        ClearHoldoff(engine);
        await engine.TickAsync(); // arms the mode debounce

        ClearReverseModeDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync(); // attempt 1: fails
        Assert.Equal(new[] { ("LSB", (int?)null) }, stubRig.SetModes);
        Assert.NotNull(engine._modePush);
        Assert.Null(engine._reverseSyncError); // not final yet -- still retrying

        stubRig.SetModeResult = true; // the rig actually caught up
        ClearPushBackoff(engine);
        ClearRigWriteGap(engine);
        ClearHoldoff(engine);
        await engine.TickAsync(); // attempt 2: succeeds

        Assert.Equal(new[] { ("LSB", (int?)null), ("LSB", (int?)null) }, stubRig.SetModes);
        Assert.Null(engine._modePush);
        Assert.Null(engine._reverseSyncError);
    }

    [Fact]
    public async Task ModeLadder_GivesUpAfterMaxAttemptsAndRevertsToRig()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false)) { SetModeResult = false };
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.Mode = "LSB";
        ClearHoldoff(engine);
        await engine.TickAsync(); // arms the mode debounce, no push yet
        Assert.Empty(stubRig.SetModes);

        for (var i = 0; i < SyncEngine.ReversePushMaxAttempts; i++)
        {
            ClearReverseModeDebounce(engine);
            ClearPushBackoff(engine);
            ClearRigWriteGap(engine);
            ClearHoldoff(engine);
            await engine.TickAsync();
        }

        Assert.Equal(SyncEngine.ReversePushMaxAttempts, stubRig.SetModes.Count(m => m == ("LSB", null)));
        Assert.Null(engine._modePush);
        Assert.NotNull(engine._reverseSyncError);
        Assert.Null(engine._lastSentModeKey);

        ClearHoldoff(engine);
        await engine.TickAsync();
        Assert.Equal(SyncEngine.ReversePushMaxAttempts, stubRig.SetModes.Count(m => m == ("LSB", null)));
    }

    [Fact]
    public async Task RapidModeSwitch_SupersedesStaleInFlightPush()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false)) { SetModeResult = false };
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.Mode = "CW";
        ClearHoldoff(engine);
        await engine.TickAsync(); // arms debounce for CW
        ClearReverseModeDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync(); // attempt 1 for CW: fails, ladder now in flight
        Assert.Equal(new[] { ("CW", (int?)null) }, stubRig.SetModes);
        Assert.NotNull(engine._modePush);
        Assert.Equal("CW", engine._modePush.Target);

        status.Mode = "LSB";
        ClearHoldoff(engine);
        await engine.TickAsync(); // drops the stale CW ladder; arms LSB's debounce instead

        Assert.Null(engine._modePush);
        Assert.Equal(new[] { ("CW", (int?)null) }, stubRig.SetModes);
    }

    [Fact]
    public void ReverseSyncPendingText_BlankOnFirstAttemptShownFromSecond()
    {
        var engine = MakeEngine();
        Assert.Null(engine.ReverseSyncPendingText());

        engine._modePush = new ReversePush("LSB") { Attempts = 0 };
        Assert.Null(engine.ReverseSyncPendingText()); // attempt 1 in flight -- still quiet

        engine._modePush = new ReversePush("LSB") { Attempts = 1 };
        var text = engine.ReverseSyncPendingText();
        Assert.NotNull(text);
        Assert.Contains("LSB", text);
        Assert.Contains($"2/{SyncEngine.ReversePushMaxAttempts}", text);
    }

    [Fact]
    public async Task RigWriteRateLimiter_BlocksADifferentAxisWriteAfterAFailedPushToo()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false)) { SetModeResult = false };
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.Mode = "LSB";
        ClearHoldoff(engine);
        await engine.TickAsync(); // arms the mode debounce
        ClearReverseModeDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync(); // attempt 1: fails -- does NOT touch _forwardPushCompletedAt
        Assert.Equal(new[] { ("LSB", (int?)null) }, stubRig.SetModes);
        Assert.NotNull(engine._modePush); // still retrying, not given up yet

        status.FreqHz = 14_200_000;
        ClearHoldoff(engine);
        await engine.TickAsync(); // arms the freq debounce
        ClearReverseDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync(); // debounce elapsed, otherwise eligible -- but blocked by the write-rate floor
        Assert.Empty(stubRig.SetFreqs);

        ClearRigWriteGap(engine);
        ClearHoldoff(engine);
        await engine.TickAsync(); // gap elapsed -- now goes through
        Assert.Equal(new[] { 14_200_000 }, stubRig.SetFreqs);
    }

    [Fact]
    public async Task ReverseCwPush_EchoesRigsOwnCwVariantInsteadOfCanonicalCw()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "CW-L", 500, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.Mode = "CW"; // user clicks CW on the WebSDR page
        ClearHoldoff(engine);
        await engine.TickAsync(); // arms the mode debounce
        ClearReverseModeDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync(); // pushes -- must echo "CW-L", not "CW"

        Assert.Equal(new[] { ("CW-L", (int?)500) }, stubRig.SetModes);
    }

    [Fact]
    public async Task ReverseCwPush_FallsBackToCanonicalCwWhenRigHasNoCwFamilyMode()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.Mode = "CW"; // user clicks CW; rig is still reporting USB
        ClearHoldoff(engine);
        await engine.TickAsync();
        ClearReverseModeDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync(); // pushes -- no CW-family value to echo, sends canonical "CW"

        Assert.Equal(new[] { ("CW", (int?)null) }, stubRig.SetModes);
    }

    [Fact]
    public async Task ModeGiveupError_NamesTheStringActuallySentNotTheCanonicalMode()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "CW-L", 500, false)) { SetModeResult = false };
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.Mode = "CW";
        ClearHoldoff(engine);
        await engine.TickAsync(); // arms the mode debounce

        for (var i = 0; i < SyncEngine.ReversePushMaxAttempts; i++)
        {
            ClearReverseModeDebounce(engine);
            ClearPushBackoff(engine);
            ClearRigWriteGap(engine);
            ClearHoldoff(engine);
            await engine.TickAsync();
        }

        Assert.Equal(SyncEngine.ReversePushMaxAttempts, stubRig.SetModes.Count(m => m == ("CW-L", 500)));
        Assert.NotNull(engine._reverseSyncError);
        Assert.Contains("CW-L", engine._reverseSyncError);
        Assert.Equal("CW", engine._lastObservedModeKey);
    }

    [Fact]
    public async Task ReverseSyncHeld_SuppressesTheWholeReverseTick()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);

        engine.ApplyReverseSyncHeld(true);
        ClearHoldoff(engine);
        await engine.TickAsync();

        Assert.Empty(stubRig.SetFreqs);
        Assert.Empty(stubRig.SetModes);
        Assert.False(engine._reverseBaselineCaptured); // never even seeded while held
    }

    [Fact]
    public async Task ReverseSyncHeld_BlocksAnAlreadyDebouncedEligiblePush()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.Mode = "LSB";
        ClearHoldoff(engine);
        await engine.TickAsync(); // arms the mode debounce
        ClearReverseModeDebounce(engine);

        engine.ApplyReverseSyncHeld(true);
        ClearHoldoff(engine);
        await engine.TickAsync(); // would have pushed -- held instead

        Assert.Empty(stubRig.SetModes);
    }

    [Fact]
    public async Task ReverseSyncHeld_ClearsInFlightLadderOnReleaseAndResumesCleanly()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false)) { SetModeResult = false };
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.Mode = "LSB";
        ClearHoldoff(engine);
        await engine.TickAsync(); // arms the mode debounce
        ClearReverseModeDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync(); // attempt 1: fails, ladder now in flight
        Assert.NotNull(engine._modePush);

        engine.ApplyReverseSyncHeld(true);
        Assert.Null(engine._modePush); // cleared on engaging Hold
        Assert.False(engine._reverseBaselineCaptured);

        engine.ApplyReverseSyncHeld(false);
        Assert.Null(engine._modePush); // still clear on release -- nothing to resume
        Assert.False(engine._reverseBaselineCaptured);

        ClearHoldoff(engine);
        await engine.TickAsync();
        Assert.True(engine._reverseBaselineCaptured);
        Assert.Equal(new[] { ("LSB", (int?)null) }, stubRig.SetModes);
    }

    [Fact]
    public async Task ReverseSyncRangeGuard_RejectsFrequencyAboveMax()
    {
        var engine = MakeEngine();
        engine._settings.ReverseSyncMaxHz = 30_000_000;
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.FreqHz = 35_000_000; // above the configured max
        ClearHoldoff(engine);
        await engine.TickAsync(); // arms the freq debounce
        ClearReverseDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync(); // debounce elapsed -- rejected by the range guard, not pushed

        Assert.Empty(stubRig.SetFreqs);
        Assert.Null(engine._freqPush);
        Assert.NotNull(engine._reverseSyncError);
        Assert.Contains("outside", engine._reverseSyncError, StringComparison.OrdinalIgnoreCase);
        Assert.Equal(35_000_000, engine._lastObservedFreq);
        Assert.Null(engine._lastSentFreq);
    }

    [Fact]
    public async Task ReverseSyncRangeGuard_RejectsFrequencyBelowMin()
    {
        var engine = MakeEngine();
        engine._settings.ReverseSyncMinHz = 1_800_000;
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.FreqHz = 500_000; // below the configured min
        ClearHoldoff(engine);
        await engine.TickAsync();
        ClearReverseDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync();

        Assert.Empty(stubRig.SetFreqs);
        Assert.NotNull(engine._reverseSyncError);
    }

    [Fact]
    public async Task ReverseSyncRangeGuard_AllowsInRangeFrequency()
    {
        var engine = MakeEngine();
        engine._settings.ReverseSyncMinHz = 1_800_000;
        engine._settings.ReverseSyncMaxHz = 30_000_000;
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.FreqHz = 14_200_000; // well inside the configured range
        ClearHoldoff(engine);
        await engine.TickAsync();
        ClearReverseDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync();

        Assert.Equal(new[] { 14_200_000 }, stubRig.SetFreqs);
        Assert.Null(engine._reverseSyncError);
    }

    [Fact]
    public async Task ReverseSyncRangeGuard_UnrestrictedByDefault()
    {
        var engine = MakeEngine();
        Assert.Null(engine._settings.ReverseSyncMinHz);
        Assert.Null(engine._settings.ReverseSyncMaxHz);
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.FreqHz = 1_000_000_000; // absurdly high -- still allowed, no range configured
        ClearHoldoff(engine);
        await engine.TickAsync();
        ClearReverseDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync();

        Assert.Equal(new[] { 1_000_000_000 }, stubRig.SetFreqs);
    }

    [Fact]
    public async Task HoldEngagedMidAwait_DiscardsTheStalePushsGiveupError()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false)) { SetModeResult = false };
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.Mode = "LSB";
        ClearHoldoff(engine);
        await engine.TickAsync(); // arms the mode debounce

        // Burn all but the final attempt, so the next one is precisely the
        // attempt that would otherwise write the give-up error.
        for (var i = 0; i < SyncEngine.ReversePushMaxAttempts - 1; i++)
        {
            ClearReverseModeDebounce(engine);
            ClearPushBackoff(engine);
            ClearRigWriteGap(engine);
            ClearHoldoff(engine);
            await engine.TickAsync();
        }

        Assert.NotNull(engine._modePush);
        Assert.Equal(SyncEngine.ReversePushMaxAttempts - 1, engine._modePush.Attempts);
        Assert.Null(engine._reverseSyncError); // not final yet

        var enteredSetMode = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        stubRig.SetModeOverride = async (modeName, passbandHz, _) =>
        {
            stubRig.SetModes.Add((modeName, passbandHz));
            enteredSetMode.TrySetResult();
            await Task.Delay(20); // real suspension, like the verify-readback loop
            return false;
        };

        ClearReverseModeDebounce(engine);
        ClearPushBackoff(engine);
        ClearRigWriteGap(engine);
        ClearHoldoff(engine);
        var tick = engine.TickAsync();
        await enteredSetMode.Task;
        engine.ApplyReverseSyncHeld(true); // user presses Hold mid-write
        await tick;

        Assert.True(engine._reverseSyncHeld);
        Assert.Null(engine._modePush); // cancelled, not resurrected by the stale continuation
        Assert.Null(engine._reverseSyncError); // no give-up message for a push the user cancelled
    }

    [Fact]
    public async Task ResetSyncLatches_DoesNotReleaseHold()
    {
        var engine = MakeEngine();
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);

        engine.ApplyReverseSyncHeld(true);
        engine.ResetSyncLatches();

        Assert.True(engine._reverseSyncHeld);
        status.FreqHz = 14_200_000;
        ClearHoldoff(engine);
        await engine.TickAsync();
        ClearReverseDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync();
        Assert.Empty(stubRig.SetFreqs);
    }

    [Fact]
    public async Task RangeGuard_ChecksTheRigNativeFrequencyNotThePageFrequency()
    {
        // Allowed: page value is above max, rig-native value is not.
        var engine = MakeEngine();
        engine._settings.ReverseSyncMaxHz = 30_000_000;
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        (engine._rig, engine._rigActive) = (stubRig, true);
        (engine._driver, engine._websdrActive) = (new CwOffsetReverseDriver(status), true);
        await SettleAndCaptureBaselineAsync(engine);

        status.FreqHz = 30_000_500; // rig-native: 29_999_800, inside the range
        ClearHoldoff(engine);
        await engine.TickAsync();
        ClearReverseDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync();

        Assert.Equal(new[] { 30_000_500 - CwOffsetHz }, stubRig.SetFreqs);
        Assert.Null(engine._reverseSyncError);

        // Rejected: page value is above min, rig-native value is below it.
        engine = MakeEngine();
        engine._settings.ReverseSyncMinHz = 1_800_000;
        stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        status = new WebSDRStatus(true, 14074000, "USB");
        (engine._rig, engine._rigActive) = (stubRig, true);
        (engine._driver, engine._websdrActive) = (new CwOffsetReverseDriver(status), true);
        await SettleAndCaptureBaselineAsync(engine);

        status.FreqHz = 1_800_300; // rig-native: 1_799_600, below the configured min
        ClearHoldoff(engine);
        await engine.TickAsync();
        ClearReverseDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync();

        Assert.Empty(stubRig.SetFreqs);
        Assert.NotNull(engine._reverseSyncError);
        Assert.Contains((1_800_300 - CwOffsetHz).ToString(), engine._reverseSyncError);
    }

    [Fact]
    public async Task RangeGuard_AllowsAFrequencyExactlyOnBothBounds()
    {
        var engine = MakeEngine();
        engine._settings.ReverseSyncMinHz = 14_200_000;
        engine._settings.ReverseSyncMaxHz = 14_200_000;
        var stubRig = new StubReverseRig(new RigState(14074000, "USB", 2700, false));
        var status = new WebSDRStatus(true, 14074000, "USB");
        var stubDriver = new StubReverseDriver(status);
        (engine._rig, engine._rigActive, engine._driver, engine._websdrActive) = (stubRig, true, stubDriver, true);
        await SettleAndCaptureBaselineAsync(engine);

        status.FreqHz = 14_200_000; // exactly on both bounds
        ClearHoldoff(engine);
        await engine.TickAsync();
        ClearReverseDebounce(engine);
        ClearHoldoff(engine);
        await engine.TickAsync();

        Assert.Equal(new[] { 14_200_000 }, stubRig.SetFreqs);
        Assert.Null(engine._reverseSyncError);
    }
}
