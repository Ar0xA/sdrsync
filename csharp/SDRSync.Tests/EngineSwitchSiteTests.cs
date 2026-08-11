using SDRSync.Core;
using SDRSync.Rig;
using SDRSync.Sync;
using SDRSync.WebSdr;
using Xunit;

namespace SDRSync.Tests;

/// <summary>
/// Ported from tests/test_engine_switch_site.py. The rig and WebSDR
/// subsystems have independent lifecycles: picking a different WebSDR must
/// never touch the rigctld connection. The reverse is NOT symmetric
/// (deliberately, since v9): stopping the rig also stops an active WebSDR
/// session, since WebSDR has nothing driving its sync without a rig.
///
/// Also covers the v14 attach-supervisor redesign (flap-vs-genuine-drop
/// distinction, jittered/clamped retry ladder, preflight-gate skip-the-
/// expensive-attach-when-deep) and the v14 idle-disconnect feature
/// (activity tracking that ignores dropped/None reads per-field, auto-
/// resume on rig activity, rate-limited retry, and the "was the
/// idle-stopped site since edited/deleted/never-saved" resume paths).
///
/// A note on WebSDRDriverRegistry.Drivers: it's a process-wide static
/// dictionary (SDRSync.WebSdr/IWebSDRDriver.cs), the C# analog of Python's
/// engine_module.DRIVERS dict that those tests monkeypatch per-test (pytest
/// auto-reverts monkeypatch after each test; xUnit has no equivalent).
/// xUnit runs the methods of one test class sequentially by default, so
/// each test here simply (re)assigns the "websdr_org"/"kiwisdr" keys it
/// needs -- but WebSdrRegistryTests (added in step 5) also writes to this
/// same dictionary via RegisterBuiltinDrivers(), and xUnit runs DIFFERENT
/// classes in parallel by default, so both classes share the
/// "WebSDRDriverRegistry" collection (see WebSdrTestHelpers.cs) to pin
/// them to run sequentially relative to each other -- confirmed necessary
/// live, not a defensive guess: omitting this caused an intermittent
/// InvalidCastException here from a real WebsdrOrgDriver factory clobbering
/// this class's StubDriver one mid-run.
/// </summary>
[Collection("WebSDRDriverRegistry")]
public class EngineSwitchSiteTests
{
    private class StubDriver(string url) : IWebSDRDriver
    {
        public string Url { get; } = url;
        public bool Attached => true;
        public bool Closed { get; private set; }

        public Task AttachAsync(IWebSdrPage page) => Task.CompletedTask;

        public Task<bool> TuneHzAsync(int freqHz, bool verify = true) => Task.FromResult(true);

        public Task<bool> SetModeAsync(string hamlibMode, int? passbandHz) => Task.FromResult(true);

        public Task SetMutedAsync(bool muted) => Task.CompletedTask;

        public Task<WebSDRStatus> GetStatusAsync() => Task.FromResult(new WebSDRStatus(true));

        public string? HamlibModeFromStatus(WebSDRStatus status) => null;

        public int? RigFreqFromStatus(WebSDRStatus status) => null;

        public Task CloseAsync()
        {
            Closed = true;
            return Task.CompletedTask;
        }
    }

    /// <summary>Just enough of IRigClient's interface to prove CloseAsync() was (or wasn't) called.</summary>
    private sealed class StubRig : IRigClient
    {
        public bool Closed { get; private set; }

        public Task<bool> EnsureConnectedAsync() => Task.FromResult(true);
        public Task<RigState> GetStateAsync() => Task.FromResult(new RigState(null, null, null, null));

        public Task CloseAsync()
        {
            Closed = true;
            return Task.CompletedTask;
        }

        public Task<bool> SetFreqAsync(int freqHz, double? verifyBudgetS = null) => Task.FromResult(true);
        public Task<bool> SetModeAsync(string modeName, int? passbandHz, double? verifyBudgetS = null) => Task.FromResult(true);
    }

    /// <summary>Sentinel page -- nothing in these tests touches its content, only identity (via TrackingWebViewHost's Created/Destroyed lists).</summary>
    private sealed class StubPage : IWebSdrPage
    {
        public Action<string>? OnDead { get; private set; }

        public void SetOnDead(Action<string> onDead) => OnDead = onDead;

        public Task NavigateAsync(string url, double timeoutS) => Task.CompletedTask;
        public Task<System.Text.Json.JsonElement?> EvaluateAsync(string js, params object?[] args) => Task.FromResult<System.Text.Json.JsonElement?>(null);
        public Task WaitForFunctionAsync(string js, double timeoutS) => Task.CompletedTask;
        public Task ClickAsync(int x, int y) => Task.CompletedTask;
        public void OnConsole(Action<ConsoleMessage> handler) { }
        public void OnPageError(Action<Exception> handler) { }
        public Task CloseAsync() => Task.CompletedTask;
    }

    /// <summary>Tracks created/destroyed pages so tests can assert on WebView lifecycle without a real Avalonia WebView existing.</summary>
    private sealed class TrackingWebViewHost : IWebViewHost
    {
        public List<StubPage> Created { get; } = new();
        public List<StubPage> Destroyed { get; } = new();

        /// <summary>
        /// Settable override standing in for the Python tests' instance-
        /// method monkeypatch (`engine._webview_host.create_page = ...`) --
        /// C# can't reassign a method on an instance, so this stub checks
        /// for an override delegate first. Used to simulate CreatePageAsync
        /// raising (the real failure path idle-resume retry tests guard
        /// against) without swapping the whole host object out from under
        /// the engine.
        /// </summary>
        public Func<Action<string>?, Task<IWebSdrPage>>? CreatePageOverride { get; set; }

        public Task<IWebSdrPage> CreatePageAsync(Action<string>? onDead = null)
        {
            if (CreatePageOverride is not null) return CreatePageOverride(onDead);
            var page = new StubPage();
            if (onDead is not null) page.SetOnDead(onDead);
            Created.Add(page);
            return Task.FromResult<IWebSdrPage>(page);
        }

        public Task DestroyPageAsync(IWebSdrPage page)
        {
            Destroyed.Add((StubPage)page);
            return Task.CompletedTask;
        }
    }

    private static SyncEngine MakeEngine()
    {
        var engine = new SyncEngine(new AppSettings(), new TrackingWebViewHost());
        engine._attachSupervisor = NoopAttachSupervisorAsync;
        return engine;
    }

    private static Task NoopAttachSupervisorAsync(IWebSdrPage page, CancellationToken ct) => Task.CompletedTask;

    private static TrackingWebViewHost Host(SyncEngine engine) => (TrackingWebViewHost)engine._webviewHost;

    [Fact]
    public async Task StartWebsdr_DoesNotTouchInactiveRig()
    {
        WebSDRDriverRegistry.Drivers["websdr_org"] = (url, _) => new StubDriver(url);
        var engine = MakeEngine();
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.StartWebsdrAsync(site);

        Assert.True(engine._websdrActive);
        Assert.False(engine._rigActive);
        Assert.Null(engine._rig);
    }

    [Fact]
    public async Task SwitchingSite_SwapsDriverAndResetsLatchesWithoutTouchingRig()
    {
        WebSDRDriverRegistry.Drivers["websdr_org"] = (url, _) => new StubDriver(url);
        WebSDRDriverRegistry.Drivers["kiwisdr"] = (url, _) => new StubDriver(url);
        var engine = MakeEngine();
        var siteA = new SiteEntry("A", "http://a.invalid/", "websdr_org");
        var siteB = new SiteEntry("B", "http://b.invalid/", "kiwisdr");

        await engine.StartWebsdrAsync(siteA);
        var originalDriver = (StubDriver)engine._driver!;
        Assert.Same(siteA, engine._site);
        var originalRig = engine._rig; // null -- rig was never started

        engine._lastSentFreq = 14074000;
        engine._lastSentModeKey = ("USB", 2700);
        engine._lastPtt = false;

        await engine.StartWebsdrAsync(siteB); // the "switch" -- same call as connect

        Assert.True(originalDriver.Closed);
        Assert.Same(siteB, engine._site);
        Assert.NotSame(originalDriver, engine._driver);
        Assert.Equal("http://b.invalid/", ((StubDriver)engine._driver!).Url);
        Assert.Null(engine._lastSentFreq);
        Assert.Null(engine._lastSentModeKey);
        Assert.Null(engine._lastPtt);
        Assert.Same(originalRig, engine._rig); // still untouched
        // Switching sites must tear down the first WebView, not leak it.
        Assert.Equal(2, Host(engine).Created.Count);
        Assert.Single(Host(engine).Destroyed);
    }

    [Fact]
    public async Task SwitchWebsdr_ReusesTheSamePageWithoutDestroyingIt()
    {
        // SwitchWebsdrAsync is the actual "Switch" path -- unlike
        // StartWebsdrAsync's own "if active, stop first" behavior exercised
        // by SwitchingSite_SwapsDriverAndResetsLatchesWithoutTouchingRig
        // above, this must NOT create a second WebView or destroy the
        // first: that destroy-and-recreate cycle was the root of a string
        // of Win32 z-order/visibility bugs against the always-visible
        // WebSDR window.
        WebSDRDriverRegistry.Drivers["websdr_org"] = (url, _) => new StubDriver(url);
        WebSDRDriverRegistry.Drivers["kiwisdr"] = (url, _) => new StubDriver(url);
        var engine = MakeEngine();
        var siteA = new SiteEntry("A", "http://a.invalid/", "websdr_org");
        var siteB = new SiteEntry("B", "http://b.invalid/", "kiwisdr");

        await engine.StartWebsdrAsync(siteA);
        var originalDriver = (StubDriver)engine._driver!;
        var originalPage = engine._page;

        engine._lastSentFreq = 14074000;
        engine._lastSentModeKey = ("USB", 2700);
        engine._lastPtt = false;

        await engine.SwitchWebsdrAsync(siteB);

        Assert.True(originalDriver.Closed);
        Assert.Same(siteB, engine._site);
        Assert.NotSame(originalDriver, engine._driver);
        Assert.Equal("http://b.invalid/", ((StubDriver)engine._driver!).Url);
        Assert.Null(engine._lastSentFreq);
        Assert.Null(engine._lastSentModeKey);
        Assert.Null(engine._lastPtt);
        Assert.True(engine._websdrActive);
        // The whole point: same page, nothing created or destroyed.
        Assert.Same(originalPage, engine._page);
        Assert.Single(Host(engine).Created);
        Assert.Empty(Host(engine).Destroyed);
    }

    [Fact]
    public async Task SwitchWebsdr_FallsBackToAFullStartWithNoExistingPage()
    {
        // The GUI only ever calls this while already active, but a
        // defensive fallback matters more than an assertion here would.
        WebSDRDriverRegistry.Drivers["websdr_org"] = (url, _) => new StubDriver(url);
        var engine = MakeEngine();
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.SwitchWebsdrAsync(site);

        Assert.True(engine._websdrActive);
        Assert.Same(site, engine._site);
        Assert.Single(Host(engine).Created);
    }

    [Fact]
    public async Task SwitchWebsdr_RebindsOnDeadSoAPostSwitchCrashStillRecovers()
    {
        // Regression guard for the exact bug SetOnDead() exists to
        // prevent: the dead-callback closure captures a generation number
        // by value, and SwitchWebsdrAsync() bumps that generation without
        // going through CreatePageAsync() (which is what normally rewires
        // OnDead for a new generation). Without the explicit rebind, a
        // WebView crash AFTER a switch would report the pre-switch
        // generation, OnPageDead() would see it doesn't match the current
        // one, and silently ignore a real crash instead of recovering.
        WebSDRDriverRegistry.Drivers["websdr_org"] = (url, _) => new StubDriver(url);
        WebSDRDriverRegistry.Drivers["kiwisdr"] = (url, _) => new StubDriver(url);
        var engine = MakeEngine();
        engine._started = true; // OnPageDead() is a no-op before Start() -- see ThreadSafeEntryPointsAreNoopBeforeRun
        var siteA = new SiteEntry("A", "http://a.invalid/", "websdr_org");
        var siteB = new SiteEntry("B", "http://b.invalid/", "kiwisdr");

        await engine.StartWebsdrAsync(siteA);
        var page = (StubPage)engine._page!;
        await engine.SwitchWebsdrAsync(siteB);
        Assert.NotNull(page.OnDead);
        // Simulate the page dying after the switch -- same call shape a
        // real page adapter uses (a single reason string). Deliberately
        // calling the REBOUND callback itself (not engine.OnPageDead()
        // with an explicit generation) -- the whole point is proving the
        // closure captured the post-switch generation on its own.
        page.OnDead!("script timeout");
        // OnPageDead schedules HandlePageDead via SingleThreadedLoop.Post()
        // on the engine's real background pump thread -- give it time to
        // actually run (see PageDeath_RecreatesTheWebsdrSession).
        await Task.Delay(50);

        Assert.Equal(2, Host(engine).Created.Count);
        Assert.Equal(siteB.Url, ((StubDriver)engine._driver!).Url);
    }

    [Fact]
    public async Task StopWebsdr_DoesNotTouchRig()
    {
        WebSDRDriverRegistry.Drivers["websdr_org"] = (url, _) => new StubDriver(url);
        var engine = MakeEngine();
        engine._rigActive = true; // pretend the rig subsystem is up
        var rigStub = new StubRig();
        engine._rig = rigStub;

        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.StartWebsdrAsync(site);
        Assert.True(engine._websdrActive);
        await engine.StopWebsdrAsync();
        Assert.False(engine._websdrActive);
        Assert.Null(engine._site);
        Assert.Single(Host(engine).Destroyed);
        // The whole point: stopping WebSDR never touches the rig.
        Assert.Same(rigStub, engine._rig);
        Assert.False(rigStub.Closed);
        Assert.True(engine._rigActive);
    }

    [Fact]
    public async Task StopRig_AlsoStopsAnActiveWebsdrSession()
    {
        // v9: deliberate one-directional exception to the independent-
        // lifecycles principle -- see the class doc comment above.
        WebSDRDriverRegistry.Drivers["websdr_org"] = (url, _) => new StubDriver(url);
        var engine = MakeEngine();
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.StartWebsdrAsync(site);
        var driver = (StubDriver)engine._driver!;
        engine._rigActive = true;
        var rigStub = new StubRig();
        engine._rig = rigStub;

        await engine.StopRigAsync();

        Assert.False(engine._rigActive);
        Assert.Null(engine._rig);
        Assert.True(rigStub.Closed);
        Assert.False(engine._websdrActive);
        Assert.Null(engine._driver);
        Assert.True(driver.Closed);
    }

    [Fact]
    public async Task StopRig_LeavesAnInactiveWebsdrSessionAlone()
    {
        // Confirms StopRigAsync() doesn't unconditionally call
        // StopWebsdrAsync() in a way that would raise/misbehave when
        // there's nothing to stop.
        var engine = MakeEngine();
        engine._rigActive = true;
        var rigStub = new StubRig();
        engine._rig = rigStub;

        await engine.StopRigAsync(); // must not raise

        Assert.False(engine._rigActive);
        Assert.False(engine._websdrActive);
    }

    [Fact]
    public void ThreadSafeEntryPoints_AreNoopBeforeRun()
    {
        // _loop is only started once Start() runs; calling any thread-safe
        // entry point before that must not raise, and must not schedule
        // work that's never awaited (SingleThreadedLoop.Post() is a
        // fire-and-forget enqueue that's safe to call before RunAsync()).
        var engine = new SyncEngine(new AppSettings(), new TrackingWebViewHost());
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        engine.StartWebsdrFromOtherThread(site); // must not raise
        engine.StopWebsdrFromOtherThread();
        engine.StartRigFromOtherThread("rigctld", "127.0.0.1", 4532, true);
        engine.StopRigFromOtherThread();
    }

    [Fact]
    public async Task PageDeath_RecreatesTheWebsdrSession()
    {
        // A WebView dying (script timeout with no way to cancel it) must
        // trigger a fresh StartWebsdrAsync(), the same recovery path an
        // explicit reconnect takes -- this was a real gap the Python
        // original's own implementation review found (nothing previously
        // called the callback at all).
        WebSDRDriverRegistry.Drivers["websdr_org"] = (url, _) => new StubDriver(url);
        var engine = MakeEngine();
        engine._started = true; // OnPageDead() is a no-op before Start() -- see ThreadSafeEntryPointsAreNoopBeforeRun
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.StartWebsdrAsync(site);
        Assert.Single(Host(engine).Created);
        var generation = engine._websdrGeneration;

        engine.OnPageDead(generation, "test-induced death");
        // OnPageDead schedules HandlePageDead via SingleThreadedLoop.Post()
        // on the engine's real background pump thread (SingleThreadedLoop
        // always runs one, unlike asyncio's single-threaded loop the
        // Python original posts onto) -- give it real wall-clock time to
        // actually run.
        await Task.Delay(50);

        Assert.Equal(2, Host(engine).Created.Count);
        Assert.True(engine._websdrActive);
    }

    [Fact]
    public async Task StalePageDeathNotification_IsIgnored()
    {
        // A dead-notification tagged with an old generation (from a page
        // that's already been replaced/torn down) must not trigger a
        // spurious recreation of whatever's active now.
        WebSDRDriverRegistry.Drivers["websdr_org"] = (url, _) => new StubDriver(url);
        var engine = MakeEngine();
        engine._started = true;
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.StartWebsdrAsync(site);
        var staleGeneration = engine._websdrGeneration;
        await engine.StopWebsdrAsync();

        engine.OnPageDead(staleGeneration, "stale");
        await Task.Delay(50);

        Assert.False(engine._websdrActive);
        Assert.Single(Host(engine).Created); // no spurious recreation
    }

    // ---------------------------------------------------------------- v14 attach supervisor

    private sealed class ScriptedAttachDriver(string url = "http://a.invalid/") : IWebSDRDriver
    {
        public string Url { get; } = url;
        public bool Attached { get; set; }
        public int AttachCalls { get; private set; }
        public Exception? AttachRaises { get; set; }

        public Task AttachAsync(IWebSdrPage page)
        {
            AttachCalls++;
            if (AttachRaises is not null) throw AttachRaises;
            Attached = true;
            return Task.CompletedTask;
        }

        public Task<bool> TuneHzAsync(int freqHz, bool verify = true) => Task.FromResult(true);
        public Task<bool> SetModeAsync(string hamlibMode, int? passbandHz) => Task.FromResult(true);
        public Task SetMutedAsync(bool muted) => Task.CompletedTask;
        public Task<WebSDRStatus> GetStatusAsync() => Task.FromResult(new WebSDRStatus(Attached));
        public string? HamlibModeFromStatus(WebSDRStatus status) => null;
        public int? RigFreqFromStatus(WebSDRStatus status) => null;
        public Task CloseAsync() => Task.CompletedTask;
    }

    /// <summary>
    /// Drives AttachSupervisorAsync() for a bounded number of loop
    /// iterations with no real sleeping: SleepOrStopAsync is replaced by a
    /// recorder that also ends the loop once `iterations` sleeps have
    /// happened. onSleep(n) runs after each sleep, letting a test change
    /// the world between iterations (e.g. drop the attachment).
    /// </summary>
    private static List<double> RunSupervisor(SyncEngine engine, IWebSDRDriver driver, int iterations, Action<int>? onSleep = null)
    {
        var delays = new List<double>();

        Task FakeSleep(double delay, CancellationToken ct)
        {
            delays.Add(delay);
            onSleep?.Invoke(delays.Count);
            if (delays.Count >= iterations) engine._websdrActive = false;
            return Task.CompletedTask;
        }

        engine._sleepOrStop = FakeSleep;
        engine._driver = driver;
        engine._websdrActive = true;
        // The real method off the class, not engine._attachSupervisor --
        // MakeEngine() replaces that instance delegate with a no-op stub
        // for the lifecycle tests above, and these tests are specifically
        // about the supervisor itself.
        engine.AttachSupervisorAsync(new StubPage(), CancellationToken.None).GetAwaiter().GetResult();
        return delays;
    }

    [Fact]
    public void FlappingAttachment_KeepsGrowingTheFailureCounter()
    {
        // v14: an attach that succeeds and drops again before
        // AttachStableS is a flap -- exactly what a server-side fair-use
        // kick looks like from here. The old code reset the counter the
        // instant attach() returned, so the backoff never grew and the
        // client hammered the site forever at the base delay.
        var engine = MakeEngine();
        var driver = new ScriptedAttachDriver();

        void DropIt(int _) => driver.Attached = false; // the far end kicks us right back off

        RunSupervisor(engine, driver, iterations: 4, onSleep: DropIt);

        Assert.Equal(2, driver.AttachCalls);
        Assert.Equal(2, engine._attachFailures); // grew across the flap, not reset by it
    }

    [Fact]
    public void AttachmentThatStaysUpPastTheStableWindow_ResetsTheLadder()
    {
        var engine = MakeEngine();
        var driver = new ScriptedAttachDriver();
        engine._attachFailures = 4; // a rough patch before this attach
        var failuresAfterAttach = new List<int>();

        void AgeTheAttachment(int n)
        {
            if (n == 1)
            {
                // The attach in iteration 1 succeeded; pretend it has now
                // been up for longer than AttachStableS.
                failuresAfterAttach.Add(engine._attachFailures);
                engine._attachAttachedAt -= SyncEngine.AttachStableS + 1.0;
            }
        }

        RunSupervisor(engine, driver, iterations: 2, onSleep: AgeTheAttachment);

        // A bare successful attach does NOT forgive the ladder...
        Assert.Equal(new[] { 4 }, failuresAfterAttach);
        // ...but staying up past the stable window does.
        Assert.Equal(0, engine._attachFailures);
        Assert.Null(engine._attachAttachedAt);
    }

    [Fact]
    public void AttachRetryDelay_IsJitteredWithinBounds()
    {
        var engine = MakeEngine();
        engine._attachFailures = 1;
        var delays = Enumerable.Range(0, 50).Select(_ => engine.AttachRetryDelay()).ToList();

        var (low, high) = SyncEngine.AttachRetryJitter;
        var basе = SyncEngine.AttachRetryBaseDelayS;
        Assert.All(delays, d => Assert.InRange(d, basе * low, basе * high));
        Assert.True(delays.Distinct().Count() > 1); // actually jittered, not a constant
    }

    [Fact]
    public void AttachRetryDelayLadder_DoublesUpToTheExtendedCeiling()
    {
        var engine = MakeEngine();
        var (low, high) = SyncEngine.AttachRetryJitter;
        foreach (var (failures, undelayed) in new (int, double)[] { (1, 2.0), (2, 4.0), (5, 32.0), (20, SyncEngine.AttachRetryMaxDelayS) })
        {
            engine._attachFailures = failures;
            var delay = engine.AttachRetryDelay();
            Assert.InRange(delay, undelayed * low, undelayed * high);
        }

        Assert.Equal(300.0, SyncEngine.AttachRetryMaxDelayS); // 5 minutes, not 30 seconds
    }

    [Fact]
    public async Task DeepLadder_SkipsTheExpensiveAttachWhenTheCheapCheckFails()
    {
        // Once the ladder is deep, one cheap HTTP GET stands in for a full
        // page load + JS wait + WebSocket against a site that is simply
        // down.
        var engine = MakeEngine();
        engine._site = new SiteEntry("A", "http://a.invalid/", "websdr_org");
        engine._attachFailures = SyncEngine.AttachPreflightGateFailures;
        var driver = new ScriptedAttachDriver();

        var checkedUrls = new List<string>();
        engine._checkWebsdrUrl = url =>
        {
            checkedUrls.Add(url);
            return Task.FromResult((false, $"Could not reach WebSDR site {url}"));
        };

        RunSupervisor(engine, driver, iterations: 1);

        Assert.Equal(new[] { "http://a.invalid/" }, checkedUrls);
        Assert.Equal(0, driver.AttachCalls); // the expensive attempt was skipped entirely
        // A failed reachability check IS a failed attempt at the far end,
        // so the ladder keeps growing rather than freezing at this rung.
        Assert.Equal(SyncEngine.AttachPreflightGateFailures + 1, engine._attachFailures);
    }

    [Fact]
    public void ShallowLadder_DoesNotRunTheCheapCheck()
    {
        // The gate is for deep retries only -- a first/second attempt must
        // not pay for an extra HTTP round trip.
        var engine = MakeEngine();
        engine._site = new SiteEntry("A", "http://a.invalid/", "websdr_org");
        engine._attachFailures = SyncEngine.AttachPreflightGateFailures - 1;
        var driver = new ScriptedAttachDriver();

        var checkedUrls = new List<string>();
        engine._checkWebsdrUrl = url =>
        {
            checkedUrls.Add(url);
            return Task.FromResult((false, "down"));
        };

        RunSupervisor(engine, driver, iterations: 1);

        Assert.Empty(checkedUrls);
        Assert.Equal(1, driver.AttachCalls);
    }

    [Fact]
    public void StillRetryingStatus_AppearsOnceTheLadderIsDeep()
    {
        var engine = MakeEngine();
        engine._attachFailures = SyncEngine.AttachStillTryingFailures - 2;
        engine.RegisterAttachFailure();
        Assert.Null(engine._attachStatusMessage); // not yet worth surfacing

        engine.RegisterAttachFailure();
        var message = engine._attachStatusMessage;
        Assert.NotNull(message);
        Assert.Contains("still retrying", message); // never reads like a crash or a give-up
    }

    [Fact]
    public async Task PageDeath_DoesNotResetTheAttachFailureLadder()
    {
        // The laundering path this closes: HandlePageDead ->
        // StartWebsdrAsync() previously started a brand-new supervisor
        // task whose failure count began at zero, so a site failing every
        // few seconds was retried forever at the base delay.
        WebSDRDriverRegistry.Drivers["websdr_org"] = (url, _) => new StubDriver(url);
        var engine = MakeEngine();
        engine._started = true; // OnPageDead() is a no-op before Start() -- see ThreadSafeEntryPointsAreNoopBeforeRun
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.StartWebsdrAsync(site);
        engine._attachFailures = 4;
        var generation = engine._websdrGeneration;

        engine.OnPageDead(generation, "test-induced death");
        await Task.Delay(50);

        Assert.Equal(2, Host(engine).Created.Count); // still recovers...
        Assert.Equal(4, engine._attachFailures); // ...without forgiving the ladder
    }

    [Fact]
    public async Task UserInitiatedStart_DoesResetTheAttachFailureLadder()
    {
        // Contrast to the test above: a real Connect/Switch click is a
        // fresh human decision and gets a clean ladder.
        WebSDRDriverRegistry.Drivers["websdr_org"] = (url, _) => new StubDriver(url);
        var engine = MakeEngine();
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        engine._attachFailures = 4;
        engine._attachStatusMessage = "WebSDR site not responding...";
        await engine.StartWebsdrAsync(site); // defaults to userInitiated=true
        Assert.Equal(0, engine._attachFailures);
        Assert.Null(engine._attachStatusMessage);
    }

    // ---------------------------------------------------------------- idle disconnect

    /// <summary>Enough of IRigClient for TickAsync(): state is mutated by the test to simulate the operator touching (or not touching) the radio. Reachable simulates the CAT link itself dropping (USB unplugged, rigctld killed, PC slept).</summary>
    private sealed class IdleStubRig(RigState state) : IRigClient
    {
        public RigState State { get; set; } = state;
        public bool Reachable { get; set; } = true;

        public Task<bool> EnsureConnectedAsync() => Task.FromResult(Reachable);
        public Task<RigState> GetStateAsync() => Task.FromResult(State);
        public Task CloseAsync() => Task.CompletedTask;
        public Task<bool> SetFreqAsync(int freqHz, double? verifyBudgetS = null) => Task.FromResult(true);
        public Task<bool> SetModeAsync(string modeName, int? passbandHz, double? verifyBudgetS = null) => Task.FromResult(true);
    }

    private static (SyncEngine Engine, IdleStubRig Rig) MakeIdleEngine(int? idleMinutes)
    {
        WebSDRDriverRegistry.Drivers["websdr_org"] = (url, _) => new StubDriver(url);
        var engine = MakeEngine();
        engine._settings.WebsdrIdleDisconnectMin = idleMinutes;
        var rig = new IdleStubRig(new RigState(14074000, "USB", 2700, false));
        engine._rig = rig;
        engine._rigActive = true;
        return (engine, rig);
    }

    [Fact]
    public async Task RigActivity_UpdatesTheIdleTimer()
    {
        var (engine, rig) = MakeIdleEngine(60);

        await engine.TickAsync(); // seeds the trackers
        engine._lastRigActivityAt -= 100.0;
        var stale = engine._lastRigActivityAt;

        await engine.TickAsync(); // nothing changed -- idle time keeps accruing
        Assert.Equal(stale, engine._lastRigActivityAt);

        rig.State = new RigState(14200000, "USB", 2700, false);
        await engine.TickAsync();
        Assert.True(engine._lastRigActivityAt > stale);
    }

    [Fact]
    public async Task PttAlone_CountsAsRigActivity()
    {
        // Someone calling CQ on one frequency for an hour is not idle.
        var (engine, rig) = MakeIdleEngine(60);
        await engine.TickAsync();
        engine._lastRigActivityAt -= 100.0;
        var stale = engine._lastRigActivityAt;

        rig.State = new RigState(14074000, "USB", 2700, true);
        await engine.TickAsync();
        Assert.True(engine._lastRigActivityAt > stale);
    }

    [Fact]
    public async Task IdleThreshold_DisconnectsTheWebsdrAndArmsAutoResume()
    {
        var (engine, _) = MakeIdleEngine(1);
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.StartWebsdrAsync(site);
        await engine.TickAsync(); // seeds the activity trackers
        Assert.True(engine._websdrActive);

        engine._lastRigActivityAt -= 61.0; // one minute of nothing
        await engine.TickAsync();

        Assert.False(engine._websdrActive);
        Assert.True(engine._websdrIdleStopped);
        Assert.Equal(site, engine._idleStoppedSite); // captured before Site was cleared
        Assert.Single(Host(engine).Destroyed);
    }

    [Fact]
    public async Task RigActivityAfterAnIdleStop_ReconnectsAutomatically()
    {
        var (engine, rig) = MakeIdleEngine(1);
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.StartWebsdrAsync(site);
        await engine.TickAsync();
        engine._lastRigActivityAt -= 61.0;
        await engine.TickAsync();
        Assert.True(engine._websdrIdleStopped);

        // The operator touches the VFO.
        rig.State = new RigState(14200000, "USB", 2700, false);
        await engine.TickAsync();

        Assert.True(engine._websdrActive);
        Assert.False(engine._websdrIdleStopped);
        Assert.Null(engine._idleStoppedSite);
        Assert.Equal(site, engine._site);
        Assert.Equal(2, Host(engine).Created.Count);
    }

    [Fact]
    public async Task UserDisconnectAfterAnIdleStop_DoesNotAutoResume()
    {
        // A manual Disconnect must never leave the auto-resume machinery
        // armed -- the user asked for it off, and silently reconnecting to
        // someone else's receiver moments later is exactly the behavior
        // this whole feature exists to prevent.
        var (engine, rig) = MakeIdleEngine(1);
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.StartWebsdrAsync(site);
        await engine.TickAsync();
        engine._lastRigActivityAt -= 61.0;
        await engine.TickAsync();
        Assert.True(engine._websdrIdleStopped);

        await engine.StopWebsdrAsync(); // what StopWebsdrFromOtherThread runs
        Assert.False(engine._websdrIdleStopped);
        Assert.Null(engine._idleStoppedSite);

        rig.State = new RigState(14200000, "USB", 2700, false);
        await engine.TickAsync();

        Assert.False(engine._websdrActive);
        Assert.Single(Host(engine).Created); // no silent reconnection
    }

    [Fact]
    public async Task IdleDisconnectDisabled_NeverFires()
    {
        foreach (int? disabledValue in new int?[] { null, 0 })
        {
            var (engine, _) = MakeIdleEngine(disabledValue);
            var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

            await engine.StartWebsdrAsync(site);
            await engine.TickAsync();
            engine._lastRigActivityAt -= 60 * 60 * 24; // a whole day of nothing
            await engine.TickAsync();
            Assert.True(engine._websdrActive);
            Assert.False(engine._websdrIdleStopped);
        }
    }

    [Fact]
    public async Task IdleStop_IsPublishedAsAStateNotAnError()
    {
        // The GUI distinguishes "disconnected (idle)" from a failure
        // purely by this flag -- an idle release must never render
        // through the red error label.
        var (engine, _) = MakeIdleEngine(1);
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.StartWebsdrAsync(site);
        await engine.TickAsync();
        engine._lastRigActivityAt -= 61.0;
        await engine.TickAsync();

        var snapshots = new List<StatusSnapshot>();
        while (engine.StatusReader.TryRead(out var msg))
        {
            if (msg is StatusSnapshot snapshot) snapshots.Add(snapshot);
        }

        var last = snapshots[^1];
        Assert.True(last.WebsdrIdleStopped);
        Assert.False(last.WebsdrActive);
        Assert.True(last.Websdr is null || string.IsNullOrEmpty(last.Websdr.LastError));
    }

    // ------------------------------------------- idle disconnect: correctness fixes

    [Fact]
    public async Task ManualConnectAfterALongIdlePeriod_IsNotUndoneOneTickLater()
    {
        // The idle clock is keyed on RIG activity, which keeps running
        // while no WebSDR session exists. Without stamping it on a
        // user-initiated start, IdleDisconnectDue() is already true the
        // instant a manual Connect comes up and the very next tick tears
        // it straight back down -- i.e. the user cannot connect by hand at
        // all. Also covers the first-ever Connect on an app left open past
        // the threshold with the rig parked on one frequency.
        var (engine, _) = MakeIdleEngine(1);
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        // The rig is already connected and being polled -- deliberately
        // established BEFORE the idle window, so the not-connected ->
        // connected activity edge is long past and cannot mask this.
        await engine.TickAsync();
        // An hour of the rig sitting untouched before the user ever clicks
        // Connect.
        engine._lastRigActivityAt -= 3600.0;

        await engine.StartWebsdrAsync(site); // the Connect click itself
        Assert.True(engine._websdrActive);

        await engine.TickAsync();
        Assert.True(engine._websdrActive); // still up, not idle-stopped
        Assert.False(engine._websdrIdleStopped);

        await engine.TickAsync();
        Assert.True(engine._websdrActive);
    }

    [Fact]
    public async Task RigDisconnectWhileIdleStopped_DisarmsAutoResume()
    {
        // StopWebsdrAsync()'s doc comment promises every non-idle stop
        // path disarms auto-resume, but the rig-stop cascade is guarded by
        // `if (_websdrActive)` -- which is already false while
        // idle-stopped, so the cascade is skipped and the bookkeeping
        // could survive an explicit rig Disconnect. The app would then
        // silently reopen a session on someone else's receiver hours
        // later, on the first rig activity, with no Connect click
        // anywhere.
        var (engine, _) = MakeIdleEngine(1);
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.StartWebsdrAsync(site);
        await engine.TickAsync();
        engine._lastRigActivityAt -= 61.0;
        await engine.TickAsync();
        Assert.True(engine._websdrIdleStopped);
        Assert.False(engine._websdrActive); // which is why the cascade is skipped

        await engine.StopRigAsync(); // the Transceiver panel's own Disconnect
        Assert.False(engine._websdrIdleStopped);
        Assert.Null(engine._idleStoppedSite);

        // Hours later the user reconnects the rig and starts using it.
        engine._rig = new IdleStubRig(new RigState(14200000, "USB", 2700, false));
        engine._rigActive = true;
        await engine.TickAsync();
        await engine.TickAsync();

        Assert.False(engine._websdrActive);
        Assert.Single(Host(engine).Created); // no session it never asked for
    }

    [Fact]
    public async Task AFailedIdleResume_StaysArmedForALaterRetry()
    {
        // ResumeWebsdrAfterIdleAsync() clears the idle flags BEFORE
        // awaiting StartWebsdrAsync(), which has early-return failure
        // paths. If one fires, both flags are already cleared and
        // _websdrActive is false -- nothing can ever retry, and WebSDR
        // sync is silently over until a human notices. Re-arming makes
        // the next rig-activity tick try again.
        var (engine, rig) = MakeIdleEngine(1);
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.StartWebsdrAsync(site);
        await engine.TickAsync();
        engine._lastRigActivityAt -= 61.0;
        await engine.TickAsync();
        Assert.True(engine._websdrIdleStopped);

        // The WebView cannot be created this time (the real failure path
        // this guards -- CreatePageAsync() throwing).
        var host = Host(engine);
        var attemptsBeforeFailure = host.Created.Count;
        host.CreatePageOverride = _ => throw new InvalidOperationException("no WebView available");

        rig.State = new RigState(14200000, "USB", 2700, false);
        await engine.TickAsync();

        Assert.False(engine._websdrActive);
        Assert.True(engine._websdrIdleStopped); // re-armed, not silently abandoned
        Assert.Equal(site, engine._idleStoppedSite);

        // ...and once the underlying problem clears, a later activity tick
        // really does recover on its own.
        host.CreatePageOverride = null;
        engine._idleResumeRetryAt = 0.0; // skip the anti-spam gap
        rig.State = new RigState(14300000, "USB", 2700, false);
        await engine.TickAsync();

        Assert.True(engine._websdrActive);
        Assert.False(engine._websdrIdleStopped);
        Assert.Equal(attemptsBeforeFailure, attemptsBeforeFailure); // sanity: no crash above
    }

    [Fact]
    public async Task ARepeatedlyFailingIdleResume_IsRateLimited()
    {
        // The retry above is driven by rig activity, and spinning a VFO
        // produces activity several times a second -- so a resume that
        // fails every time must not mean several page-creation attempts
        // per second.
        var (engine, rig) = MakeIdleEngine(1);
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.StartWebsdrAsync(site);
        await engine.TickAsync();
        engine._lastRigActivityAt -= 61.0;
        await engine.TickAsync();

        var attempts = 0;
        Host(engine).CreatePageOverride = _ =>
        {
            attempts++;
            throw new InvalidOperationException("no WebView available");
        };

        for (var n = 0; n < 10; n++) // ten ticks of the operator working the dial
        {
            rig.State = new RigState(14200000 + n * 1000, "USB", 2700, false);
            await engine.TickAsync();
        }

        Assert.Equal(1, attempts); // not ten
        Assert.True(engine._websdrIdleStopped); // still armed for a real retry
    }

    [Fact]
    public async Task AnUnreachableRig_StillIdleDisconnects()
    {
        // A rig that dropped is arguably the STRONGEST "nobody is here"
        // signal -- but every not-connected path in TickAsync() returned
        // above the idle check, so the session held a volunteer
        // receiver's audio slot forever. Note this is the path a rig that
        // drops AFTER connecting once takes: _rigConnectDeadline is
        // cleared on first success, so the 30s give-up branch can never
        // fire again and it retries indefinitely.
        var (engine, rig) = MakeIdleEngine(1);
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.StartWebsdrAsync(site);
        await engine.TickAsync();
        Assert.Null(engine._rigConnectDeadline); // connected once already

        rig.Reachable = false; // USB unplugged / rigctld killed / PC slept
        await engine.TickAsync();
        Assert.True(engine._websdrActive); // not yet due

        engine._lastRigActivityAt -= 61.0;
        await engine.TickAsync();

        Assert.False(engine._websdrActive);
        Assert.True(engine._websdrIdleStopped);
        Assert.Equal(site, engine._idleStoppedSite);
    }

    [Fact]
    public async Task ARigThatComesBackUnchanged_StillCountsAsActivity()
    {
        // The trap in fixing the above: _idleLastFreq/_idleLastMode/
        // _idleLastPtt keep their pre-drop values across the outage, so a
        // rig that returns reporting exactly what it showed before
        // (replugged without touching the dial, PC resumed from sleep)
        // produces no detectable change and the session would never come
        // back -- trading "holds the slot forever" for "never resumes".
        // The not-connected -> connected transition itself has to count.
        var (engine, rig) = MakeIdleEngine(1);
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");
        var parked = new RigState(14074000, "USB", 2700, false);

        await engine.StartWebsdrAsync(site);
        await engine.TickAsync();

        rig.Reachable = false;
        engine._lastRigActivityAt -= 61.0;
        await engine.TickAsync();
        Assert.True(engine._websdrIdleStopped);

        // Back, reporting the IDENTICAL state it had before the outage.
        rig.State = parked;
        rig.Reachable = true;
        await engine.TickAsync();

        Assert.True(engine._websdrActive);
        Assert.False(engine._websdrIdleStopped);
        Assert.Equal(2, Host(engine).Created.Count);
    }

    [Fact]
    public async Task DroppedRigReads_DoNotKeepResettingTheIdleTimer()
    {
        // Every RigState field is nullable, and null means a DROPPED
        // READ, not a value the operator moved something to. A plain !=
        // counted each drop as activity and each recovery as a second
        // one, so a marginal USB-serial/CAT link reset the idle timer
        // continuously and idle-disconnect could never fire -- on exactly
        // the unattended, flaky-link setup it matters most for.
        var (engine, rig) = MakeIdleEngine(1);
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");
        var good = new RigState(14074000, "USB", 2700, false);
        var dropped = new RigState(null, null, null, null);

        await engine.StartWebsdrAsync(site);
        await engine.TickAsync(); // seeds the trackers from a good read

        engine._lastRigActivityAt -= 59.0; // not yet due
        var stale = engine._lastRigActivityAt;

        for (var i = 0; i < 5; i++) // the link flapping, nobody at the radio
        {
            rig.State = dropped;
            await engine.TickAsync();
            rig.State = good;
            await engine.TickAsync();
        }

        Assert.Equal(stale, engine._lastRigActivityAt); // never reset by the flapping
        Assert.True(engine._websdrActive);

        engine._lastRigActivityAt -= 2.0; // now past the threshold
        await engine.TickAsync();
        Assert.False(engine._websdrActive); // idle disconnect still fires
    }

    [Fact]
    public async Task ARealChangeSeenAfterADroppedRead_StillCounts()
    {
        // The other half: ignoring null must not make the engine deaf to
        // a genuine change that happens to arrive after a dropped read.
        var (engine, rig) = MakeIdleEngine(60);

        await engine.TickAsync();
        engine._lastRigActivityAt -= 100.0;
        var stale = engine._lastRigActivityAt;

        rig.State = new RigState(null, null, null, null);
        await engine.TickAsync();
        Assert.Equal(stale, engine._lastRigActivityAt);

        rig.State = new RigState(14200000, "USB", 2700, false);
        await engine.TickAsync();
        Assert.True(engine._lastRigActivityAt > stale);
    }

    [Fact]
    public async Task APartialRead_DoesNotMaskAChangeOnAnotherAxis()
    {
        // Compared per field, so a dropped frequency read cannot swallow
        // a real mode change arriving in the same poll.
        var (engine, rig) = MakeIdleEngine(60);

        await engine.TickAsync();
        engine._lastRigActivityAt -= 100.0;
        var stale = engine._lastRigActivityAt;

        rig.State = new RigState(null, "CW", 500, false);
        await engine.TickAsync();
        Assert.True(engine._lastRigActivityAt > stale);
        // ...and the dropped frequency did NOT overwrite the remembered
        // one, so returning to it later is correctly seen as no change.
        Assert.Equal(14074000, engine._idleLastFreq);
    }

    // ------------------------------------------- per-attachment state and site staleness

    [Fact]
    public async Task Teardown_ClearsTheAttachTimestampSoTheNextPageAttachesImmediately()
    {
        // _attachAttachedAt is per-ATTACHMENT state and must not outlive a
        // teardown -- unlike _attachFailures, which deliberately does.
        // Left set, the next session's supervisor sees it non-null on its
        // very first iteration (when the new driver naturally isn't
        // attached yet), takes the flap branch, charges a bogus failure
        // and sleeps a backoff delay BEFORE ever trying to attach the new
        // page -- delaying recovery from an unrelated WebView crash and
        // mislabelling it in the log.
        WebSDRDriverRegistry.Drivers["websdr_org"] = (url, _) => new StubDriver(url);
        var engine = MakeEngine();
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.StartWebsdrAsync(site);
        engine._attachAttachedAt = MonotonicClock.NowS(); // a live attachment
        await engine.StopWebsdrAsync();
        Assert.Null(engine._attachAttachedAt);

        // The next session's supervisor really does attempt a real attach
        // on its first pass, rather than charging a flap and backing off
        // first.
        var driver = new ScriptedAttachDriver();
        RunSupervisor(engine, driver, iterations: 1);
        Assert.Equal(1, driver.AttachCalls);
        Assert.Equal(0, engine._attachFailures);
    }

    [Fact]
    public async Task AWebviewCrash_DoesNotChargeAFlapAgainstTheReplacementPage()
    {
        // End-to-end version of the above, over the real crash-recovery
        // path (HandlePageDead -> StartWebsdrAsync(userInitiated: false),
        // which deliberately does NOT reset attach state -- so the
        // teardown is the only thing that can clear the per-attachment
        // half).
        WebSDRDriverRegistry.Drivers["websdr_org"] = (url, _) => new StubDriver(url);
        var engine = MakeEngine();
        engine._started = true; // OnPageDead() is a no-op before Start() -- see ThreadSafeEntryPointsAreNoopBeforeRun
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");

        await engine.StartWebsdrAsync(site);
        engine._attachAttachedAt = MonotonicClock.NowS(); // attached and healthy...
        engine.OnPageDead(engine._websdrGeneration, "script timeout"); // ...then the WebView dies
        await Task.Delay(50);

        Assert.Equal(2, Host(engine).Created.Count); // recovered
        Assert.Null(engine._attachAttachedAt); // no phantom attachment carried over
        Assert.Equal(0, engine._attachFailures); // the crash is not a flap
    }

    [Fact]
    public void AttachRetryDelay_ExponentIsClamped()
    {
        // Unbounded exponent growth is not merely wasteful: at ~1024
        // consecutive failures, an unclamped exponent overflows a double
        // computing AttachRetryBaseDelayS * 2^(n-1), which propagates out
        // of RegisterAttachFailure() and kills the attach supervisor task
        // outright -- after which nothing ever retries, silently. At the
        // 300s ceiling that is only ~3.5 days against a site that has
        // been removed, on an app explicitly meant to run unattended
        // indefinitely.
        var engine = MakeEngine();
        var (low, high) = SyncEngine.AttachRetryJitter;
        var ceiling = SyncEngine.AttachRetryMaxDelayS;

        foreach (var failures in new[] { 1024, 5000 })
        {
            engine._attachFailures = failures;
            var delay = engine.AttachRetryDelay(); // must not raise
            Assert.InRange(delay, ceiling * low, ceiling * high);
        }

        // The cap is chosen well past the rung where the ladder already
        // pins to its ceiling, so it can never act as a second, lower cap.
        engine._attachFailures = SyncEngine.BackoffExponentCap;
        Assert.True(engine.AttachRetryDelay() >= ceiling * low);
    }

    [Fact]
    public async Task IdleResume_UsesTheSitesCurrentDefinition()
    {
        // _idleStoppedSite is a value snapshot that can sit unused for
        // hours. If the user corrects that site's definition meanwhile,
        // the resume must use the CURRENT one, not the stale capture.
        WebSDRDriverRegistry.Drivers["kiwisdr"] = (url, _) => new StubDriver(url);
        var (engine, rig) = MakeIdleEngine(1);
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");
        engine._settings.UserSites = new List<SiteEntry> { new("A", "http://a.invalid/", "websdr_org") };

        await engine.StartWebsdrAsync(site);
        await engine.TickAsync();
        engine._lastRigActivityAt -= 61.0;
        await engine.TickAsync();
        Assert.True(engine._idleStoppedSiteWasListed);

        // The user fixes the driver type while the session is idle-stopped.
        engine._settings.UserSites = new List<SiteEntry> { new("A (kiwi)", "http://a.invalid/", "kiwisdr") };
        rig.State = new RigState(14200000, "USB", 2700, false);
        await engine.TickAsync();

        Assert.True(engine._websdrActive);
        Assert.Equal("kiwisdr", engine._site!.DriverType);
        Assert.Equal("A (kiwi)", engine._site!.Name);
    }

    [Fact]
    public async Task IdleResume_DisarmsIfTheSiteWasDeletedMeanwhile()
    {
        // Reconnecting to a definition the user has since deleted would
        // be acting on data they removed -- disarm and say why instead.
        var (engine, rig) = MakeIdleEngine(1);
        var site = new SiteEntry("A", "http://a.invalid/", "websdr_org");
        engine._settings.UserSites = new List<SiteEntry> { new("A", "http://a.invalid/", "websdr_org") };

        await engine.StartWebsdrAsync(site);
        await engine.TickAsync();
        engine._lastRigActivityAt -= 61.0;
        await engine.TickAsync();
        Assert.True(engine._idleStoppedSiteWasListed);

        engine._settings.UserSites = new List<SiteEntry>(); // deleted from the dropdown
        rig.State = new RigState(14200000, "USB", 2700, false);
        await engine.TickAsync();

        Assert.False(engine._websdrActive);
        Assert.False(engine._websdrIdleStopped); // disarmed, not left half-armed
        Assert.Null(engine._idleStoppedSite);
        Assert.Single(Host(engine).Created);
    }

    [Fact]
    public async Task AnUnsavedCustomUrl_StillResumesAfterAnIdleStop()
    {
        // The trap in the deletion check: a one-off Custom URL is in no
        // site list to begin with, so "not found at resume time" cannot
        // mean "deleted" on its own -- otherwise every unsaved custom
        // session would be stranded by the idle feature.
        var (engine, rig) = MakeIdleEngine(1);
        var site = new SiteEntry("Custom", "http://custom.invalid/", "websdr_org");
        Assert.Empty(engine._settings.UserSites); // never saved to the list

        await engine.StartWebsdrAsync(site);
        await engine.TickAsync();
        engine._lastRigActivityAt -= 61.0;
        await engine.TickAsync();
        Assert.False(engine._idleStoppedSiteWasListed);

        rig.State = new RigState(14200000, "USB", 2700, false);
        await engine.TickAsync();

        Assert.True(engine._websdrActive);
        Assert.Equal(site, engine._site); // the snapshot is all there is, and it is correct
    }
}
