using SDRSync.Core;
using SDRSync.Sync;
using Xunit;

namespace SDRSync.Tests;

/// <summary>
/// Ported from tests/test_engine_poll_interval.py. PollLoopAsync must
/// honor AppSettings.PollIntervalS and pick up live changes (the GUI
/// updates it without requiring a reconnect).
///
/// Adaptation from the Python original: those tests monkeypatch
/// SyncEngine._tick to a call-counting stub, which has no direct C#
/// equivalent against a sealed class. Every TickAsync() call -- including
/// the cheap "rig not active" early-return path exercised here -- always
/// publishes exactly one StatusSnapshot, so counting messages drained from
/// the public StatusReader channel is an equivalent, black-box-observable
/// proxy for tick count, without needing to reach into a private method.
/// </summary>
public class EnginePollIntervalTests
{
    private static SyncEngine MakeEngine(double pollIntervalS) =>
        new(new AppSettings { PollIntervalS = pollIntervalS }, new StubWebViewHost());

    private static int DrainCount(SyncEngine engine)
    {
        var count = 0;
        while (engine.StatusReader.TryRead(out _)) count++;
        return count;
    }

    [Fact]
    public async Task PollLoop_TicksFasterWithAShorterInterval()
    {
        var engine = MakeEngine(0.02);

        var pollTask = engine.PollLoopAsync();
        await Task.Delay(150);
        engine._stopTcs.TrySetResult();
        await pollTask;

        // ~0.15s / 0.02s tick interval -- comfortably more than a 0.2s
        // default interval would ever produce in the same window.
        Assert.True(DrainCount(engine) >= 4);
    }

    [Fact]
    public async Task PollLoop_PicksUpALiveSettingsChange()
    {
        // The GUI updates settings.PollIntervalS directly (no engine
        // restart) -- PollLoopAsync must read it fresh each iteration.
        // A change only takes effect once the *current* wait completes,
        // not mid-wait, so this waits out a full old-interval cycle
        // before checking the new, faster rate kicked in.
        var settings = new AppSettings { PollIntervalS = 0.1 }; // starts slower
        var engine = new SyncEngine(settings, new StubWebViewHost());

        var pollTask = engine.PollLoopAsync();
        await Task.Delay(30);
        var ticksWhileSlow = DrainCount(engine);
        settings.PollIntervalS = 0.02; // GUI-style live update
        await Task.Delay(350);
        engine._stopTcs.TrySetResult();
        await pollTask;
        var ticksAfter = DrainCount(engine);

        Assert.True(ticksWhileSlow <= 1); // only the immediate first tick so far
        Assert.True(ticksAfter >= 5); // sped up once the setting change was picked up
    }

    [Fact]
    public void PollIntervalField_DefaultsMatchPreviousHardcodedValue()
    {
        Assert.Equal(0.2, new AppSettings().PollIntervalS);
    }
}
