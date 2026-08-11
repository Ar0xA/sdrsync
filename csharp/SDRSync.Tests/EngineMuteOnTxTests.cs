using SDRSync.Core;
using SDRSync.Rig;
using SDRSync.Sync;
using SDRSync.WebSdr;
using Xunit;

namespace SDRSync.Tests;

/// <summary>
/// Ported from tests/test_engine_mute_on_tx.py. Toggling mute_on_tx off
/// while still muted from an earlier PTT transition must actually unmute,
/// not leave the WebSDR muted forever -- the original _tick() gated BOTH
/// the mute and unmute calls behind the *current* mute_on_tx value, so
/// unchecking it mid-transmission skipped the falling edge's unmute call
/// entirely, with no future PTT edge able to clear it. Fixed by always
/// unmuting on the falling edge regardless of the current setting.
/// </summary>
public class EngineMuteOnTxTests
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

    private sealed class StubDriver : IWebSDRDriver
    {
        public bool Attached => true;
        public List<bool> MuteCalls { get; } = new();

        public Task AttachAsync(IWebSdrPage page) => Task.CompletedTask;

        public Task<bool> TuneHzAsync(int freqHz, bool verify = true) => Task.FromResult(true);

        public Task<bool> SetModeAsync(string hamlibMode, int? passbandHz) => Task.FromResult(true);

        public Task SetMutedAsync(bool muted)
        {
            MuteCalls.Add(muted);
            return Task.CompletedTask;
        }

        public Task<WebSDRStatus> GetStatusAsync() => Task.FromResult(new WebSDRStatus(Connected: true));

        public string? HamlibModeFromStatus(WebSDRStatus status) => null;

        public int? RigFreqFromStatus(WebSDRStatus status) => null;

        public Task CloseAsync() => Task.CompletedTask;
    }

    [Fact]
    public async Task UnmuteStillFires_AfterMuteOnTxDisabledMidTransmission()
    {
        var settings = new AppSettings { MuteOnTx = true };
        var engine = new SyncEngine(settings, new StubWebViewHost());
        var stubDriver = new StubDriver();
        var stubRig = new StubRig(new RigState(14074000, "USB", 2700, true));
        engine._rig = stubRig;
        engine._rigActive = true;
        engine._driver = stubDriver;
        engine._websdrActive = true;

        await engine.TickAsync(); // PTT rising edge, mute_on_tx true -> mutes
        settings.MuteOnTx = false; // user unchecks the box mid-TX
        stubRig.State = new RigState(14074000, "USB", 2700, false);
        // Mute/unmute share the global WebsdrMinWriteGapS floor with
        // forward pushes -- back-to-back ticks with no wall-clock delay
        // would otherwise be throttled.
        engine._lastWebsdrWriteAt -= SyncEngine.WebsdrMinWriteGapS + 0.1;
        await engine.TickAsync(); // PTT falling edge -- must still unmute

        Assert.Equal(new[] { true, false }, stubDriver.MuteCalls);
    }

    [Fact]
    public async Task MuteStaysGatedOff_WhenMuteOnTxDisabledBeforeTransmission()
    {
        var settings = new AppSettings { MuteOnTx = false };
        var engine = new SyncEngine(settings, new StubWebViewHost());
        var stubDriver = new StubDriver();
        engine._rig = new StubRig(new RigState(14074000, "USB", 2700, true));
        engine._rigActive = true;
        engine._driver = stubDriver;
        engine._websdrActive = true;

        await engine.TickAsync();

        // mute_on_tx was already off before the rising edge -- must not mute.
        Assert.Empty(stubDriver.MuteCalls);
    }

    [Fact]
    public async Task PttEdgeDeferredByWriteGap_StillFiresOnALaterTick()
    {
        // A PTT edge that arrives inside the write-gap window must be
        // DEFERRED, never dropped -- _lastPtt is deliberately not latched
        // unless the call actually goes out. Dropping it would resurrect
        // the bug this suite exists for.
        var settings = new AppSettings { MuteOnTx = true };
        var engine = new SyncEngine(settings, new StubWebViewHost());
        var stubDriver = new StubDriver();
        var stubRig = new StubRig(new RigState(14074000, "USB", 2700, true));
        engine._rig = stubRig;
        engine._rigActive = true;
        engine._driver = stubDriver;
        engine._websdrActive = true;

        await engine.TickAsync(); // rising edge -> mutes, and stamps the write gap
        Assert.Equal(new[] { true }, stubDriver.MuteCalls);

        // Falling edge arrives while the gap is still closed.
        stubRig.State = new RigState(14074000, "USB", 2700, false);
        await engine.TickAsync();
        Assert.Equal(new[] { true }, stubDriver.MuteCalls); // deferred...
        Assert.True(engine._lastPtt); // ...and NOT latched away

        // Next tick past the gap: the unmute still happens.
        engine._lastWebsdrWriteAt -= SyncEngine.WebsdrMinWriteGapS + 0.1;
        await engine.TickAsync();
        Assert.Equal(new[] { true, false }, stubDriver.MuteCalls);
        Assert.False(engine._lastPtt);
    }
}
