using System.Threading.Channels;
using Microsoft.Extensions.Logging;
using SDRSync.Core;
using SDRSync.Rig;
using SDRSync.WebSdr;

namespace SDRSync.Sync;

/// <summary>
/// Bidirectional sync engine: rig (rigctld or flrig) &lt;-&gt; WebSDR, ported
/// from sdrsync/sync/engine.py.
///
/// The rig connection and the WebSDR connection are deliberately
/// independent subsystems with independent lifecycles -- picking a
/// different WebSDR has nothing to do with the transceiver, and
/// (re)connecting the transceiver has nothing to do with which WebSDR is
/// loaded. A single SyncEngine instance owns both, for the whole lifetime
/// of the GUI session -- individual "Connect" actions from the GUI only
/// start/stop one subsystem at a time via the thread-safe *FromOtherThread
/// methods below, never the whole engine.
///
/// Threading model: the whole engine runs on one dedicated
/// <see cref="SingleThreadedLoop"/> (the C# analog of asyncio's single-
/// threaded event loop -- see that class's doc comment for why a custom
/// SynchronizationContext was hand-rolled instead of depending on the
/// long-unmaintained Nito.AsyncEx.Context package). The GUI thread never
/// touches engine state directly; it only calls the *FromOtherThread
/// methods (which post work onto the loop) and drains
/// <see cref="StatusReader"/> (a Channel, replacing Python's plain
/// queue.Queue).
/// </summary>
public sealed class SyncEngine
{
    // ------------------------------------------------------------------ constants
    public const double FreqDebounceS = 0.2;
    // 0, not a small positive jitter margin: the same constant gated BOTH
    // "is this reading different enough to become a new pending candidate"
    // AND "is the candidate different enough from what was last sent to
    // bother pushing", both with a strict >. A change at-or-below a
    // positive threshold never even registered as a candidate, so it
    // wasn't filtered once -- it got permanently stuck (accumulating
    // invisibly) until enough small changes crossed the threshold
    // cumulatively, producing a "nothing happens, then it suddenly jumps"
    // symptom live users reported. FreqDebounceS already does the actual
    // job of coalescing a burst of rapid small changes into one push.
    public const int FreqChangeThresholdHz = 0;
    public const double FullResyncIntervalS = 30.0;
    public const double WebsdrMinWriteGapS = 0.5;
    public const double ForwardPushBackoffBaseS = 1.0;
    public const double ForwardPushBackoffMaxS = 30.0;
    public const int ForwardPushImmediateRetryMaxFailures = 3;
    public const int BackoffExponentCap = 16;
    public const double AttachRetryBaseDelayS = 2.0;
    public const double AttachRetryMaxDelayS = 300.0;
    internal static readonly (double Low, double High) AttachRetryJitter = (0.8, 1.2);
    public const double AttachStableS = 30.0;
    public const int AttachPreflightGateFailures = 3;
    public const int AttachStillTryingFailures = 5;
    public const double AttachCheckIntervalS = 1.0;
    public const double IdleResumeRetryGapS = 30.0;
    public const double RigConnectTimeoutS = 10.0;

    public const double ReverseHoldoffS = 1.0;
    public const double ReverseFreqDebounceS = 0.5;
    // 0 -- see FreqChangeThresholdHz's comment; same stuck-candidate bug,
    // same fix, mirrored for the reverse (WebSDR -> rig) direction.
    public const int ReverseFreqChangeThresholdHz = 0;
    public const double ReverseModeDebounceS = 0.35;
    public const double ReversePushAttemptVerifyS = 1.0;
    public const int ReversePushMaxAttempts = 2;
    internal static readonly double[] ReversePushBackoffS = { 0.35 };
    public const double ReverseMinRigWriteGapS = 0.35;

    // ------------------------------------------------------------------ fields
    internal readonly AppSettings _settings;
    internal readonly IWebViewHost _webviewHost;
    internal readonly Channel<GuiMessage> _statusChannel = Channel.CreateUnbounded<GuiMessage>();
    internal readonly SingleThreadedLoop _loop;
    internal readonly Random _random = new();

    internal volatile bool _started;
    internal volatile bool _stopRequested;
    internal readonly TaskCompletionSource _stopTcs = new(TaskCreationOptions.RunContinuationsAsynchronously);

    // Rig subsystem.
    internal IRigClient? _rig;
    internal IMockRigServer? _mockServer;
    internal IMockRigState? _mockState;
    internal bool _rigActive;
    internal string? _rigError;
    internal string? _rigBackend;
    internal string? _rigHost;
    internal int? _rigPort;
    internal double? _rigConnectDeadline;

    // Bumped as the FIRST action of every StartRigAsync() call -- including
    // a same-tick mock-bind failure -- so every snapshot published from one
    // attempt (through its eventual give-up) shares one generation, and a
    // fresh Connect click's first snapshot always carries a strictly
    // greater one. Without this, a GUI status-queue backlog from a FAILED
    // attempt can still be draining at the exact moment the user clicks
    // Connect again, re-popping a stale error for an attempt that hasn't
    // started -- see StatusSnapshot.RigGeneration's doc comment for the
    // GUI-side half of this fix.
    internal int _rigGeneration;

    // WebSDR subsystem.
    internal IWebSdrPage? _page;
    internal IWebSDRDriver? _driver;
    internal SiteEntry? _site;
    internal Task? _attachTask;
    internal CancellationTokenSource? _attachCts;
    internal bool _websdrActive;
    internal int _websdrGeneration;
    internal int _attachFailures;
    internal double? _attachAttachedAt;
    internal double? _attachFirstFailureAt;
    internal string? _attachStatusMessage;

    // v14 idle disconnect.
    internal double _lastRigActivityAt = MonotonicClock.NowS();
    internal int? _idleLastFreq;
    internal string? _idleLastMode;
    internal bool? _idleLastPtt;
    internal bool _websdrIdleStopped;
    internal SiteEntry? _idleStoppedSite;
    internal bool _idleStoppedSiteWasListed;
    internal double _idleResumeRetryAt;
    internal bool _rigWasConnectedLastTick;

    // Forward sync dedupe latches.
    internal int? _lastSentFreq;
    internal int? _pendingFreq;
    internal double _pendingFreqSince;
    internal (string Mode, int? PassbandHz)? _lastSentModeKey;
    internal bool? _lastPtt;
    internal double _lastFullResyncAt = MonotonicClock.NowS();
    internal double _lastWebsdrWriteAt;
    internal readonly ForwardPushBackoff _forwardFreqBackoff = new();
    internal readonly ForwardPushBackoff _forwardModeBackoff = new();
    internal int? _lastPushedToWebsdrFreq;
    internal string? _lastPushedToWebsdrMode;
    internal double _forwardPushCompletedAt;
    internal bool _reverseReseedDue;
    internal int _forwardLatchGeneration;

    // v11 reverse sync latches.
    internal int? _lastObservedFreq;
    internal int? _pendingReverseFreq;
    internal double _pendingReverseFreqSince;
    internal string? _lastObservedModeKey;
    internal bool _reverseBaselineCaptured;
    internal string? _lastUnmappedReverseMode;
    internal ReversePush? _modePush;
    internal ReversePush? _freqPush;
    internal double _lastRigWriteAt;
    internal string? _pendingReverseMode;
    internal double _pendingReverseModeSince;
    internal string? _reverseSyncError;
    internal int _syncLatchGeneration;
    internal volatile bool _reverseSyncHeld;
    internal volatile bool _forwardSyncPaused;

    // Testability seams: instance-level overrides mirroring the Python
    // original's own test pattern of monkeypatching engine._attach_supervisor
    // / engine._sleep_or_stop (bound-method instance overrides) and
    // engine_module.check_websdr_url (a module-level function reference the
    // engine resolves at call time). C# has no equivalent of either -- an
    // instance method can't be reassigned, and a static call is not
    // interceptable -- so these default to the real implementation and exist
    // solely so tests can substitute a fake without spinning up a real
    // attach cycle, a real Task.Delay, or a real HTTP request.
    internal Func<IWebSdrPage, CancellationToken, Task> _attachSupervisor;
    internal Func<double, CancellationToken, Task> _sleepOrStop;
    internal Func<string, Task<(bool Ok, string Message)>> _checkWebsdrUrl;

    public SyncEngine(AppSettings settings, IWebViewHost webviewHost)
    {
        _settings = settings;
        _webviewHost = webviewHost;
        _loop = new SingleThreadedLoop("sdrsync-engine");
        _attachSupervisor = AttachSupervisorAsync;
        _sleepOrStop = SleepOrStopAsync;
        _checkWebsdrUrl = url => WebSdrPreflight.CheckWebsdrUrlAsync(url);
    }

    public ChannelReader<GuiMessage> StatusReader => _statusChannel.Reader;

    // ------------------------------------------------------------------ thread-safe entry points
    /// <summary>Starts the engine loop -- mirrors `asyncio.run(engine.run())` running on its own dedicated background thread in the Python original's app.py.</summary>
    public Task Start() => _loop.RunAsync(RunAsync);

    /// <summary>Ends the whole session (app window closing), tearing down both subsystems.</summary>
    public void StopFromOtherThread()
    {
        _stopRequested = true;
        if (_started)
        {
            _loop.Post(() =>
            {
                _stopTcs.TrySetResult();
                return Task.CompletedTask;
            });
        }
    }

    public void StartRigFromOtherThread(string backend, string host, int port, bool useMock)
    {
        if (!_started) return;
        _loop.Post(() => StartRigAsync(backend, host, port, useMock));
    }

    public void StopRigFromOtherThread()
    {
        if (!_started) return;
        _loop.Post(() => StopRigAsync());
    }

    /// <summary>
    /// The initial Connect (or a recovery from a dead page) -- if a
    /// session is already active, tears the whole page down and rebuilds
    /// it. SwitchWebsdrFromOtherThread is the entry point for changing
    /// sites while already connected; it does NOT go through this.
    /// </summary>
    public void StartWebsdrFromOtherThread(SiteEntry site)
    {
        if (!_started) return;
        _loop.Post(() => StartWebsdrAsync(site));
    }

    /// <summary>Loads a different site on the SAME already-open page instead of tearing it down and rebuilding it.</summary>
    public void SwitchWebsdrFromOtherThread(SiteEntry site)
    {
        if (!_started) return;
        _loop.Post(() => SwitchWebsdrAsync(site));
    }

    /// <summary>A manual Disconnect -- disarms the v14 idle auto-resume (idleDisconnect defaults false in StopWebsdrAsync).</summary>
    public void StopWebsdrFromOtherThread()
    {
        if (!_started) return;
        _loop.Post(() => StopWebsdrAsync());
    }

    // ------------------------------------------------------------------ publish
    internal void Publish(bool? rigConnected = null, int? rigFreqHz = null, string? rigMode = null, bool? rigPtt = null, WebSDRStatus? websdr = null)
    {
        var effectiveWebsdr = websdr;
        if (effectiveWebsdr is not null && _attachStatusMessage is not null && !effectiveWebsdr.Connected)
        {
            effectiveWebsdr = effectiveWebsdr with { LastError = _attachStatusMessage };
        }

        var snapshot = new StatusSnapshot
        {
            RigGeneration = _rigGeneration,
            RigActive = _rigActive,
            RigConnected = rigConnected ?? false,
            RigFreqHz = rigFreqHz,
            RigMode = rigMode,
            RigPtt = rigPtt,
            RigError = _rigError,
            RigConnectRemainingS = _rigConnectDeadline is not null ? Math.Max(0.0, _rigConnectDeadline.Value - MonotonicClock.NowS()) : null,
            WebsdrActive = _websdrActive,
            Websdr = effectiveWebsdr,
            ReverseSyncError = _reverseSyncError,
            ReverseSyncPending = ReverseSyncPendingText(),
            ReverseSyncHeld = _reverseSyncHeld,
            ForwardSyncPaused = _forwardSyncPaused,
            WebsdrIdleStopped = _websdrIdleStopped,
        };
        _statusChannel.Writer.TryWrite(snapshot);
    }

    internal string? ReverseSyncPendingText()
    {
        if (_modePush is not null && _modePush.Attempts >= 1)
        {
            return $"Setting mode '{_modePush.Target}' on the rig... (attempt {_modePush.Attempts + 1}/{ReversePushMaxAttempts})";
        }

        if (_freqPush is not null && _freqPush.Attempts >= 1)
        {
            return $"Setting frequency {_freqPush.Target} Hz on the rig... (attempt {_freqPush.Attempts + 1}/{ReversePushMaxAttempts})";
        }

        return null;
    }

    /// <summary>Reserved for the whole background loop dying unexpectedly.</summary>
    public void PublishFatalError(string message) =>
        _statusChannel.Writer.TryWrite(new StatusSnapshot { FatalError = message });

    // ------------------------------------------------------------------ run
    internal async Task RunAsync()
    {
        _started = true;
        if (_stopRequested)
        {
            CoreLog.Logger.LogInformation("Stop was requested before the engine started; exiting immediately");
            return;
        }

        try
        {
            await PollLoopAsync();
        }
        finally
        {
            await StopWebsdrAsync();
            await StopRigAsync();
        }
    }

    // ------------------------------------------------------------------ rig
    /// <summary>
    /// backend is "rigctld" or "flrig" -- a simple if/else, not a
    /// registry (there are only ever exactly 2, unlike the WebSDR side's
    /// driver registry). Mock mode is always server-side only: even in
    /// mock mode, _rig is the real client class for the selected backend,
    /// pointed at the just-started embedded mock server -- never a
    /// separate fake client.
    /// </summary>
    internal async Task StartRigAsync(string backend, string host, int port, bool useMock)
    {
        _rigGeneration++;

        if (_rigActive)
        {
            await StopRigAsync();
        }

        if (useMock)
        {
            try
            {
                if (backend == "flrig")
                {
                    var server = await FakeFlrigServer.StartAsync(host, port);
                    _mockServer = server;
                    _mockState = server.State;
                }
                else
                {
                    var server = await FakeRigctldServer.StartAsync(host, port);
                    _mockServer = server;
                    _mockState = server.State;
                }
            }
            catch (Exception e) when (e is IOException or System.Net.Sockets.SocketException)
            {
                _rigError = $"Could not start the embedded mock rig on {host}:{port} ({e.Message}). Is a real {backend} already running on that port?";
                CoreLog.Logger.LogError("{Error}", _rigError);
                Publish();
                return;
            }
        }

        _rig = backend == "flrig" ? new FlrigClient(host, port) : new RigctldClient(host, port);
        _rigActive = true;
        _rigError = null;
        _lastPtt = null;
        (_rigBackend, _rigHost, _rigPort) = (backend, host, port);
        _rigConnectDeadline = MonotonicClock.NowS() + RigConnectTimeoutS;
        CoreLog.Logger.LogInformation("Rig session started ({Backend}, {Host}:{Port}, mock={Mock})", backend, host, port, useMock);
        Publish();
    }

    internal async Task StopRigAsync(string? error = null)
    {
        // A WebSDR session with no rig behind it has nothing driving its
        // sync -- stopping the rig always stops WebSDR too.
        if (_websdrActive)
        {
            await StopWebsdrAsync();
        }

        // Unconditionally, outside the cascade above: while a session is
        // idle-stopped, WebsdrActive is already false, so the cascade is
        // skipped and the auto-resume bookkeeping would survive an
        // explicit rig Disconnect otherwise.
        _websdrIdleStopped = false;
        _idleStoppedSite = null;

        if (_rig is not null)
        {
            await _rig.CloseAsync();
            _rig = null;
        }

        if (_mockServer is not null)
        {
            _mockServer.Close();
            await _mockServer.WaitClosedAsync();
            _mockServer = null;
            _mockState = null;
        }

        var wasActive = _rigActive;
        _rigActive = false;
        _rigWasConnectedLastTick = false;
        _rigError = error;
        _rigConnectDeadline = null;
        if (wasActive && error is null)
        {
            CoreLog.Logger.LogInformation("Rig session stopped");
        }

        Publish();
    }

    // ------------------------------------------------------------------ websdr
    /// <summary>
    /// userInitiated=false marks an INTERNAL restart (recovering from a
    /// dead page), which must NOT reset the attach-failure ladder --
    /// that's the exact laundering path that would let a site failing
    /// every few seconds be retried forever at the base delay.
    /// </summary>
    internal async Task StartWebsdrAsync(SiteEntry site, bool userInitiated = true)
    {
        if (!WebSDRDriverRegistry.Drivers.TryGetValue(site.DriverType, out var factory))
        {
            CoreLog.Logger.LogError("No WebSDR driver registered for type {DriverType}", site.DriverType);
            Publish(websdr: new WebSDRStatus(Connected: false, LastError: $"No WebSDR driver registered for type '{site.DriverType}'"));
            return;
        }

        if (_websdrActive)
        {
            await StopWebsdrAsync();
        }

        if (userInitiated)
        {
            _attachFailures = 0;
            _attachAttachedAt = null;
            _attachFirstFailureAt = null;
            _attachStatusMessage = null;
            _websdrIdleStopped = false;
            _idleStoppedSite = null;
            // A human clicking Connect/Switch IS rig-adjacent activity --
            // without this stamp, connecting after a long idle period
            // would tear the session straight back down one poll interval
            // later.
            _lastRigActivityAt = MonotonicClock.NowS();
        }

        _websdrGeneration++;
        var myGeneration = _websdrGeneration;

        IWebSdrPage page;
        try
        {
            page = await _webviewHost.CreatePageAsync(reason => OnPageDead(myGeneration, reason));
        }
        catch (Exception e)
        {
            CoreLog.Logger.LogError(e, "Failed to create WebView for WebSDR session");
            Publish(websdr: new WebSDRStatus(Connected: false, LastError: DescribeWebviewCreateError(e)));
            return;
        }

        _page = page;
        _site = site;
        _driver = factory(site.Url, _settings.CwOffsetHz);
        _websdrActive = true;
        ResetSyncLatches();

        _attachCts = new CancellationTokenSource();
        _attachTask = _loop.PostAndTrack(() => _attachSupervisor(page, _attachCts.Token));
        CoreLog.Logger.LogInformation("WebSDR session started: {Name} ({Url})", site.Name, site.Url);
        Publish();
    }

    /// <summary>
    /// Called by the page adapter when a script timeout or other
    /// unrecoverable failure marks it permanently dead. Marshals onto the
    /// engine loop before touching any engine state -- mirrors
    /// _on_page_dead()'s wx.CallAfter -> loop.call_soon_threadsafe
    /// two-step in the Python original.
    /// </summary>
    internal void OnPageDead(int generation, string reason)
    {
        if (!_started) return;
        _loop.Post(() =>
        {
            HandlePageDead(generation, reason);
            return Task.CompletedTask;
        });
    }

    internal void HandlePageDead(int generation, string reason)
    {
        if (generation != _websdrGeneration || !_websdrActive || _site is null)
        {
            // Stale notification from a page already replaced/torn down.
            return;
        }

        CoreLog.Logger.LogWarning("WebView for {Name} died ({Reason}) -- recreating", _site.Name, reason);
        // userInitiated=false: internal crash recovery, not a human asking
        // for a fresh connection -- the attach-failure ladder must carry
        // across it.
        _ = _loop.PostAndTrack(() => StartWebsdrAsync(_site, userInitiated: false));
    }

    /// <summary>
    /// Loads a different site on the SAME already-open page instead of
    /// destroying and recreating it -- avoids the Win32 z-order/visibility
    /// races a full teardown-and-rebuild on every switch produced in the
    /// Python original's own live testing.
    /// </summary>
    internal async Task SwitchWebsdrAsync(SiteEntry site)
    {
        if (!WebSDRDriverRegistry.Drivers.TryGetValue(site.DriverType, out var factory))
        {
            CoreLog.Logger.LogError("No WebSDR driver registered for type {DriverType}", site.DriverType);
            Publish(websdr: new WebSDRStatus(Connected: false, LastError: $"No WebSDR driver registered for type '{site.DriverType}'"));
            return;
        }

        if (_page is null || !_websdrActive)
        {
            await StartWebsdrAsync(site);
            return;
        }

        _websdrGeneration++; // invalidates any in-flight OnPageDead notification
        await CancelAttachTaskAsync("switching WebSDR sites");

        if (_driver is not null)
        {
            try
            {
                await _driver.CloseAsync();
            }
            catch (Exception e)
            {
                CoreLog.Logger.LogError(e, "Error closing WebSDR driver during switch");
            }
        }

        _attachFailures = 0;
        _attachAttachedAt = null;
        _attachFirstFailureAt = null;
        _attachStatusMessage = null;
        _websdrIdleStopped = false;
        _idleStoppedSite = null;
        _lastRigActivityAt = MonotonicClock.NowS();

        _site = site;
        _driver = factory(site.Url, _settings.CwOffsetHz);
        ResetSyncLatches();
        var myGeneration = _websdrGeneration;
        _page.SetOnDead(reason => OnPageDead(myGeneration, reason));
        _attachCts = new CancellationTokenSource();
        _attachTask = _loop.PostAndTrack(() => _attachSupervisor(_page, _attachCts.Token));
        CoreLog.Logger.LogInformation("WebSDR session switched to: {Name} ({Url})", site.Name, site.Url);
        Publish();
    }

    /// <summary>idleDisconnect=true is the ONLY path that arms auto-resume. Every other stop disarms it.</summary>
    internal async Task StopWebsdrAsync(bool idleDisconnect = false)
    {
        _websdrGeneration++; // invalidates any in-flight OnPageDead notification
        await CancelAttachTaskAsync("stopping WebSDR session");

        if (_driver is not null)
        {
            try
            {
                await _driver.CloseAsync();
            }
            catch (Exception e)
            {
                CoreLog.Logger.LogError(e, "Error closing WebSDR driver");
            }

            _driver = null;
        }

        if (_page is not null)
        {
            try
            {
                await _webviewHost.DestroyPageAsync(_page);
            }
            catch (Exception e)
            {
                CoreLog.Logger.LogWarning("Non-fatal error destroying WebView: {Message}", e.Message);
            }

            _page = null;
        }

        var wasActive = _websdrActive;
        _site = null;
        _websdrActive = false;
        _attachStatusMessage = null;
        // Per-attachment state, so it must not survive a teardown -- left
        // set, the next session's supervisor would see it non-None on its
        // very first loop iteration and wrongly take the "flap" branch.
        _attachAttachedAt = null;
        _websdrIdleStopped = idleDisconnect;
        if (!idleDisconnect)
        {
            _idleStoppedSite = null;
        }

        if (wasActive)
        {
            CoreLog.Logger.LogInformation("WebSDR session stopped");
        }

        Publish();
    }

    internal async Task CancelAttachTaskAsync(string context)
    {
        if (_attachTask is not null)
        {
            _attachCts?.Cancel();
            try
            {
                await _attachTask;
            }
            catch (OperationCanceledException)
            {
                // expected
            }
            catch (Exception e)
            {
                CoreLog.Logger.LogError(e, "Attach supervisor raised while {Context}", context);
            }

            _attachTask = null;
            _attachCts?.Dispose();
            _attachCts = null;
        }
    }

    // ------------------------------------------------------------------ idle disconnect (v14)
    /// <summary>
    /// Returns true if anything about the rig changed since the last
    /// tick. A None reading is a DROPPED READ, not a value the operator
    /// moved something to -- skipped entirely, and crucially does NOT
    /// overwrite the remembered value, so a marginal CAT link resetting
    /// continuously can't defeat idle-disconnect on exactly the
    /// unattended setup it matters most for.
    /// </summary>
    internal bool NoteRigActivity(RigState state)
    {
        var changed = false;
        if (state.FreqHz is not null && state.FreqHz != _idleLastFreq)
        {
            _idleLastFreq = state.FreqHz;
            changed = true;
        }

        if (state.Mode is not null && state.Mode != _idleLastMode)
        {
            _idleLastMode = state.Mode;
            changed = true;
        }

        if (state.Ptt is not null && state.Ptt != _idleLastPtt)
        {
            _idleLastPtt = state.Ptt;
            changed = true;
        }

        if (changed)
        {
            _lastRigActivityAt = MonotonicClock.NowS();
        }

        return changed;
    }

    internal bool IdleDisconnectDue()
    {
        var minutes = _settings.WebsdrIdleDisconnectMin;
        if (minutes is null or <= 0) return false;
        return MonotonicClock.NowS() - _lastRigActivityAt >= minutes.Value * 60;
    }

    /// <summary>
    /// Looks a URL up in the site definitions available RIGHT NOW. Returns
    /// null if the URL is in no list at all -- callers must not read that
    /// as "deleted" on its own, since an unsaved Custom URL is also in no list.
    /// </summary>
    internal SiteEntry? ResolveSiteByUrl(string url)
    {
        foreach (var entries in new[] { _settings.UserSites, _settings.ImportedSites, _settings.CuratedSites })
        {
            foreach (var entry in entries)
            {
                if (entry.Url == url)
                {
                    return new SiteEntry(string.IsNullOrEmpty(entry.Name) ? url : entry.Name, url, entry.DriverType);
                }
            }
        }

        return KnownSites.List.FirstOrDefault(s => s.Url == url);
    }

    internal async Task StopWebsdrForIdleAsync()
    {
        var minutes = _settings.WebsdrIdleDisconnectMin;
        // Captured before the stop -- StopWebsdrAsync clears _site.
        _idleStoppedSite = _site;
        _idleStoppedSiteWasListed = _site is not null && ResolveSiteByUrl(_site.Url) is not null;
        _idleResumeRetryAt = 0.0;
        CoreLog.Logger.LogInformation(
            "Disconnecting idle WebSDR session (no rig activity for {Minutes} min) -- will auto-reconnect once the rig is used again",
            minutes);
        await StopWebsdrAsync(idleDisconnect: true);
    }

    /// <summary>The DISCONNECT half of the idle check, factored out so it can run on tick paths that never reach the resume half. Returns true if a session was actually stopped.</summary>
    internal async Task<bool> IdleDisconnectIfDueAsync()
    {
        if (_websdrActive && !_websdrIdleStopped && IdleDisconnectDue())
        {
            await StopWebsdrForIdleAsync();
            return true;
        }

        return false;
    }

    internal async Task ResumeWebsdrAfterIdleAsync()
    {
        if (MonotonicClock.NowS() < _idleResumeRetryAt) return; // a previous resume attempt failed very recently

        var site = _idleStoppedSite;
        var wasListed = _idleStoppedSiteWasListed;
        _websdrIdleStopped = false;
        _idleStoppedSite = null;
        if (site is null) return;

        var current = ResolveSiteByUrl(site.Url);
        if (current is not null)
        {
            site = current;
        }
        else if (wasListed)
        {
            CoreLog.Logger.LogInformation(
                "Not resuming the idle-stopped WebSDR session: {Name} ({Url}) is no longer in the site list -- auto-resume is being disarmed",
                site.Name, site.Url);
            Publish();
            return;
        }

        CoreLog.Logger.LogInformation("Rig activity detected -- reconnecting the WebSDR session to {Name}", site.Name);
        await StartWebsdrAsync(site, userInitiated: true);
        if (!_websdrActive)
        {
            CoreLog.Logger.LogWarning(
                "Could not resume the idle-stopped WebSDR session to {Name} -- staying armed to retry on the next rig activity",
                site.Name);
            _websdrIdleStopped = true;
            _idleStoppedSite = site;
            _idleStoppedSiteWasListed = wasListed;
            _idleResumeRetryAt = MonotonicClock.NowS() + IdleResumeRetryGapS;
            Publish();
        }
    }

    // ------------------------------------------------------------------ attach supervisor
    internal double AttachRetryDelay()
    {
        var exponent = Math.Min(Math.Max(_attachFailures - 1, 0), BackoffExponentCap);
        var delay = Math.Min(AttachRetryBaseDelayS * Math.Pow(2, exponent), AttachRetryMaxDelayS);
        var jitter = AttachRetryJitter.Low + (_random.NextDouble() * (AttachRetryJitter.High - AttachRetryJitter.Low));
        return delay * jitter;
    }

    internal double RegisterAttachFailure()
    {
        _attachFailures++;
        _attachAttachedAt = null;
        _attachFirstFailureAt ??= MonotonicClock.NowS();
        var delay = AttachRetryDelay();
        UpdateAttachStatusMessage(delay);
        return delay;
    }

    internal void UpdateAttachStatusMessage(double delay)
    {
        if (_attachFailures < AttachStillTryingFailures) return;
        var elapsed = MonotonicClock.NowS() - (_attachFirstFailureAt ?? MonotonicClock.NowS());
        _attachStatusMessage =
            $"WebSDR site not responding for {FormatDuration(elapsed)} ({_attachFailures} attempts) -- still retrying, next attempt in {FormatDuration(delay)}";
    }

    internal async Task AttachSupervisorAsync(IWebSdrPage page, CancellationToken ct)
    {
        while (!_stopTcs.Task.IsCompleted && _websdrActive && !ct.IsCancellationRequested)
        {
            if (_driver is null) return; // subsystem was stopped out from under this task
            if (!_driver.Attached)
            {
                if (_attachAttachedAt is not null)
                {
                    // We DID attach, and it dropped again before it ever
                    // became stable -- a flap. Count it like a failed attach.
                    var delay = RegisterAttachFailure();
                    CoreLog.Logger.LogWarning(
                        "WebSDR attachment dropped after less than {Stable}s (attempt {Failures}) -- retrying in {Delay:F1}s",
                        AttachStableS, _attachFailures, delay);
                    await _sleepOrStop(delay, ct);
                    continue;
                }

                if (_attachFailures >= AttachPreflightGateFailures && _site is not null)
                {
                    var (ok, message) = await _checkWebsdrUrl(_site.Url);
                    if (!ok)
                    {
                        var delay = RegisterAttachFailure();
                        CoreLog.Logger.LogWarning(
                            "Skipping WebSDR attach attempt {Failures} -- reachability check failed ({Message}); retrying in {Delay:F1}s",
                            _attachFailures, message, delay);
                        await _sleepOrStop(delay, ct);
                        continue;
                    }
                }

                try
                {
                    await _driver.AttachAsync(page);
                }
                catch (OperationCanceledException)
                {
                    throw;
                }
                catch (Exception e) when (e is WebSDRIncompatibleException or BrowserException)
                {
                    var delay = RegisterAttachFailure();
                    CoreLog.Logger.LogWarning("WebSDR attach attempt {Failures} failed: {Message} -- retrying in {Delay:F1}s", _attachFailures, e.Message, delay);
                    await _sleepOrStop(delay, ct);
                    continue;
                }
                catch (Exception e)
                {
                    // Any other driver bug must not silently kill this task.
                    var delay = RegisterAttachFailure();
                    CoreLog.Logger.LogError(e, "WebSDR attach attempt {Failures} raised an unexpected error -- retrying in {Delay:F1}s", _attachFailures, delay);
                    await _sleepOrStop(delay, ct);
                    continue;
                }

                if (_attachFailures > 0)
                {
                    CoreLog.Logger.LogInformation(
                        "WebSDR attach succeeded after {Failures} failed attempt(s) -- the ladder resets once it stays up {Stable:F0}s",
                        _attachFailures, AttachStableS);
                }

                // NOT resetting _attachFailures here: a success that
                // doesn't last isn't a recovery -- see AttachStableS.
                _attachAttachedAt = MonotonicClock.NowS();
                _attachStatusMessage = null;
                // The engine may have "sent" freq/mode/ptt during the
                // outage -- those were no-ops, so the latches must clear.
                ResetSyncLatches();
            }
            else if (_attachAttachedAt is not null && MonotonicClock.NowS() - _attachAttachedAt.Value >= AttachStableS)
            {
                if (_attachFailures > 0)
                {
                    CoreLog.Logger.LogInformation("WebSDR attachment stable for {Stable:F0}s -- attach retry ladder reset", AttachStableS);
                }

                _attachFailures = 0;
                _attachFirstFailureAt = null;
                _attachStatusMessage = null;
                _attachAttachedAt = null;
            }

            await _sleepOrStop(AttachCheckIntervalS, ct);
        }
    }

    internal async Task SleepOrStopAsync(double delaySeconds, CancellationToken ct = default)
    {
        var delayTask = Task.Delay(TimeSpan.FromSeconds(Math.Max(0, delaySeconds)), ct).ContinueWith(_ => { }, TaskScheduler.Default);
        await Task.WhenAny(_stopTcs.Task, delayTask);
        ct.ThrowIfCancellationRequested();
    }

    // ------------------------------------------------------------------ mock rig control (GUI-driven)
    internal void PushToMock(Action setter)
    {
        if (_mockState is null) return;
        if (!_started) return;
        _loop.Post(() =>
        {
            setter();
            return Task.CompletedTask;
        });
    }

    public void PushMockFreq(int freqHz)
    {
        var state = _mockState;
        if (state is not null) PushToMock(() => state.FreqHz = freqHz);
    }

    public void PushMockMode(string mode, int passbandHz)
    {
        var state = _mockState;
        if (state is null) return;
        PushToMock(() =>
        {
            state.Mode = mode;
            state.PassbandHz = passbandHz;
        });
    }

    public void PushMockPtt(bool isTx)
    {
        var state = _mockState;
        if (state is not null) PushToMock(() => state.Ptt = isTx ? "1" : "0");
    }

    // ------------------------------------------------------------------ Hold / rig-is-master (GUI-driven)
    /// <summary>Pauses/resumes the WebSDR -> rig direction only -- forward sync and both connections are completely untouched.</summary>
    public void SetReverseSyncHeld(bool held)
    {
        if (!_started)
        {
            _reverseSyncHeld = held;
            return;
        }

        _loop.Post(() =>
        {
            ApplyReverseSyncHeld(held);
            return Task.CompletedTask;
        });
    }

    internal void ApplyReverseSyncHeld(bool held)
    {
        _reverseSyncHeld = held;
        _modePush = null;
        _freqPush = null;
        _pendingReverseMode = null;
        _pendingReverseModeSince = 0.0;
        _pendingReverseFreq = null;
        _pendingReverseFreqSince = 0.0;
        _reverseBaselineCaptured = false;
        _reverseSyncError = null;
        _syncLatchGeneration++;
    }

    // ------------------------------------------------------------------ Pause sync (rig -> WebSDR, GUI-driven)
    public void SetForwardSyncPausedFromOtherThread(bool paused)
    {
        if (!_started)
        {
            _forwardSyncPaused = paused;
            return;
        }

        _loop.Post(() =>
        {
            _forwardSyncPaused = paused;
            return Task.CompletedTask;
        });
    }

    // ------------------------------------------------------------------
    internal void ResetSyncLatches()
    {
        _lastSentFreq = null;
        _pendingFreq = null;
        _lastSentModeKey = null;
        _lastPtt = null;
        _lastFullResyncAt = MonotonicClock.NowS();
        _lastWebsdrWriteAt = 0.0;
        _forwardFreqBackoff.Reset();
        _forwardModeBackoff.Reset();
        _lastPushedToWebsdrFreq = null;
        _lastPushedToWebsdrMode = null;
        _forwardPushCompletedAt = MonotonicClock.NowS();
        _reverseReseedDue = false;
        _lastObservedFreq = null;
        _pendingReverseFreq = null;
        _pendingReverseFreqSince = 0.0;
        _lastObservedModeKey = null;
        _reverseBaselineCaptured = false;
        _lastUnmappedReverseMode = null;
        _modePush = null;
        _freqPush = null;
        _lastRigWriteAt = 0.0;
        _pendingReverseMode = null;
        _pendingReverseModeSince = 0.0;
        _reverseSyncError = null;
        _syncLatchGeneration++;
        _forwardLatchGeneration++;
    }

    internal async Task PollLoopAsync()
    {
        CoreLog.Logger.LogInformation("Sync engine started (bidirectional: rig <-> WebSDR)");
        while (!_stopTcs.Task.IsCompleted)
        {
            try
            {
                await TickAsync();
            }
            catch (Exception e)
            {
                CoreLog.Logger.LogError(e, "Unexpected error in sync loop tick");
            }

            await SleepOrStopAsync(_settings.PollIntervalS);
        }
    }

    // ------------------------------------------------------------------ reverse sync (WebSDR -> rig)
    /// <summary>
    /// Called once per tick by TickAsync, already gated by the caller (not
    /// transmitting, WebSDR connected, past ReverseHoldoffS since the last
    /// successful push in either direction). At most one push (mode OR
    /// freq, mode taking priority) per tick.
    /// </summary>
    internal async Task ReverseSyncTickAsync(WebSDRStatus websdrStatus, RigState state)
    {
        var generation = _syncLatchGeneration;
        var obsMode = _driver!.HamlibModeFromStatus(websdrStatus);
        var obsFreq = _driver.RigFreqFromStatus(websdrStatus);

        if (!_reverseBaselineCaptured || _reverseReseedDue)
        {
            // First observation after (re)attach/toggle-on, OR the first
            // observation after a forward push actually changed something
            // -- seed, don't push, in both cases.
            _lastObservedFreq = obsFreq;
            _lastObservedModeKey = obsMode;
            _lastPushedToWebsdrFreq = obsFreq;
            _lastPushedToWebsdrMode = obsMode;
            _reverseBaselineCaptured = true;
            _reverseReseedDue = false;
            return;
        }

        var canWriteRig = MonotonicClock.NowS() - _lastRigWriteAt >= ReverseMinRigWriteGapS;
        var modeDidWork = false;

        if (obsMode is null)
        {
            if (websdrStatus.Mode != _lastUnmappedReverseMode)
            {
                _lastUnmappedReverseMode = websdrStatus.Mode;
                CoreLog.Logger.LogDebug("WebSDR mode {Mode} has no reverse hamlib mapping; frequency reverse-sync continues", websdrStatus.Mode);
            }
        }
        else
        {
            // The page moved on to a different mode while a push was still
            // being retried -- the in-flight target is stale, drop it
            // rather than keep chasing a value the user already clicked past.
            if (_modePush is not null && obsMode != (string)_modePush.Target)
            {
                _modePush = null;
            }

            if (obsMode != _pendingReverseMode)
            {
                _pendingReverseMode = obsMode;
                _pendingReverseModeSince = MonotonicClock.NowS();
            }

            if (obsMode != _lastPushedToWebsdrMode && obsMode != _lastObservedModeKey)
            {
                if (_modePush is null && MonotonicClock.NowS() - _pendingReverseModeSince >= ReverseModeDebounceS)
                {
                    _modePush = new ReversePush(obsMode);
                }

                var push = _modePush;
                if (push is not null && canWriteRig && MonotonicClock.NowS() >= push.NextAttemptAt)
                {
                    // CW is the one mode family where multiple rig-native
                    // names collapse to the same WebSDR-observable value
                    // ("CW") -- echo the rig's own current CW-variant name
                    // back instead of forcing the canonical "CW" when the
                    // rig is already in some CW-family mode.
                    var modeToSend = obsMode;
                    if (obsMode == "CW" && state.Mode is not null && state.Mode.ToUpperInvariant().StartsWith("CW", StringComparison.Ordinal))
                    {
                        modeToSend = state.Mode;
                    }

                    // passbandHz is computed for observability/test purposes
                    // only -- neither RigctldClient nor FlrigClient's
                    // SetModeAsync ever puts it on the wire (see their doc
                    // comments); a mode change here is mode-only, always.
                    var passbandHz = modeToSend == state.Mode ? state.PassbandHz : null;
                    var ok = await _rig!.SetModeAsync(modeToSend, passbandHz, ReversePushAttemptVerifyS);
                    _lastRigWriteAt = MonotonicClock.NowS();
                    if (generation != _syncLatchGeneration) return; // superseded by a concurrent reset while awaiting
                    modeDidWork = true;
                    push.Attempts++;
                    if (ok)
                    {
                        _lastPushedToWebsdrMode = obsMode;
                        _lastObservedModeKey = obsMode;
                        _forwardPushCompletedAt = MonotonicClock.NowS();
                        _reverseSyncError = null;
                        _modePush = null;
                    }
                    else if (push.Attempts >= ReversePushMaxAttempts)
                    {
                        CoreLog.Logger.LogWarning(
                            "Rig did not confirm mode {ModeToSend} after {Attempts} attempts -- reverting WebSDR to match the rig's actual mode {RigMode}",
                            modeToSend, push.Attempts, state.Mode);
                        _reverseSyncError = $"Could not set mode '{modeToSend}' on the rig -- WebSDR reverted to match the rig";
                        _lastObservedModeKey = obsMode;
                        _lastSentModeKey = null;
                        _modePush = null;
                    }
                    else
                    {
                        push.NextAttemptAt = MonotonicClock.NowS() + ReversePushBackoffS[Math.Min(push.Attempts - 1, ReversePushBackoffS.Length - 1)];
                    }
                }
            }
            else
            {
                _lastObservedModeKey = obsMode;
                _modePush = null;
            }
        }

        if (modeDidWork) return; // per-tick budget cap: mode takes priority over frequency this tick
        if (obsFreq is null) return;

        var now = MonotonicClock.NowS();
        if (_pendingReverseFreq is null || Math.Abs(obsFreq.Value - _pendingReverseFreq.Value) > ReverseFreqChangeThresholdHz)
        {
            _pendingReverseFreq = obsFreq;
            _pendingReverseFreqSince = now;
            return;
        }

        if (now - _pendingReverseFreqSince < ReverseFreqDebounceS) return;

        if ((_lastPushedToWebsdrFreq is not null && Math.Abs(_pendingReverseFreq.Value - _lastPushedToWebsdrFreq.Value) <= ReverseFreqChangeThresholdHz)
            || (_lastObservedFreq is not null && Math.Abs(_pendingReverseFreq.Value - _lastObservedFreq.Value) <= ReverseFreqChangeThresholdHz))
        {
            _freqPush = null;
            return;
        }

        var pendingFreq = _pendingReverseFreq.Value;
        var minHz = _settings.ReverseSyncMinHz;
        var maxHz = _settings.ReverseSyncMaxHz;
        if ((minHz is not null && pendingFreq < minHz) || (maxHz is not null && pendingFreq > maxHz))
        {
            CoreLog.Logger.LogWarning(
                "Reverse-sync frequency {Freq} Hz is outside the configured range [{Min}, {Max}] Hz -- rejected, reverting WebSDR to match the rig",
                pendingFreq, minHz?.ToString() ?? "-inf", maxHz?.ToString() ?? "+inf");
            _reverseSyncError = $"Frequency {pendingFreq} Hz is outside the configured reverse-sync range -- WebSDR reverted to match the rig";
            _lastObservedFreq = pendingFreq;
            _lastSentFreq = null;
            _freqPush = null;
            return;
        }

        if (_freqPush is null || (int)_freqPush.Target != pendingFreq)
        {
            _freqPush = new ReversePush(pendingFreq);
        }

        var freqPush = _freqPush;
        if (!canWriteRig || now < freqPush.NextAttemptAt) return;

        var freqOk = await _rig!.SetFreqAsync(pendingFreq, ReversePushAttemptVerifyS);
        _lastRigWriteAt = MonotonicClock.NowS();
        if (generation != _syncLatchGeneration) return; // superseded by a concurrent reset while awaiting
        freqPush.Attempts++;
        if (freqOk)
        {
            _lastPushedToWebsdrFreq = pendingFreq;
            _forwardPushCompletedAt = MonotonicClock.NowS();
            _reverseSyncError = null;
            _freqPush = null;
        }
        else if (freqPush.Attempts >= ReversePushMaxAttempts)
        {
            CoreLog.Logger.LogWarning(
                "Rig did not confirm frequency {Freq} Hz after {Attempts} attempts -- reverting WebSDR to match the rig's actual frequency {RigFreq} Hz",
                pendingFreq, freqPush.Attempts, state.FreqHz);
            _reverseSyncError = $"Could not set frequency {pendingFreq} Hz on the rig -- WebSDR reverted to match the rig";
            _lastObservedFreq = pendingFreq;
            _lastSentFreq = null;
            _freqPush = null;
        }
        else
        {
            freqPush.NextAttemptAt = MonotonicClock.NowS() + ReversePushBackoffS[Math.Min(freqPush.Attempts - 1, ReversePushBackoffS.Length - 1)];
        }
    }

    // ------------------------------------------------------------------ tick
    /// <summary>Runs one poll iteration. A no-op (beyond publishing a status snapshot) for whichever subsystem isn't active.</summary>
    internal async Task TickAsync()
    {
        var websdrStatus = _websdrActive && _driver is not null ? await _driver.GetStatusAsync() : null;

        if (!_rigActive || _rig is null)
        {
            _rigWasConnectedLastTick = false;
            if (await IdleDisconnectIfDueAsync())
            {
                websdrStatus = null;
            }

            Publish(rigConnected: false, websdr: websdrStatus);
            return;
        }

        if (!await _rig.EnsureConnectedAsync())
        {
            _rigWasConnectedLastTick = false;
            if (_rigConnectDeadline is not null && MonotonicClock.NowS() > _rigConnectDeadline.Value)
            {
                var giveUpMessage = $"Could not connect to {_rigBackend} at {_rigHost}:{_rigPort} within {RigConnectTimeoutS:F0}s, giving up";
                CoreLog.Logger.LogWarning("{Message}", giveUpMessage);
                await StopRigAsync(error: giveUpMessage);
                return;
            }

            if (await IdleDisconnectIfDueAsync())
            {
                websdrStatus = null;
            }

            Publish(rigConnected: false, websdr: websdrStatus);
            return;
        }

        _rigConnectDeadline = null;
        var rigReconnected = !_rigWasConnectedLastTick;
        _rigWasConnectedLastTick = true;
        if (rigReconnected)
        {
            _lastRigActivityAt = MonotonicClock.NowS();
        }

        var state = await _rig.GetStateAsync();

        var rigActivity = NoteRigActivity(state);
        if (rigReconnected)
        {
            rigActivity = true;
        }

        if (_websdrIdleStopped && rigActivity)
        {
            await ResumeWebsdrAfterIdleAsync();
        }
        else if (await IdleDisconnectIfDueAsync())
        {
            Publish(rigConnected: true, rigFreqHz: state.FreqHz, rigMode: state.Mode, rigPtt: state.Ptt);
            return;
        }

        if (_websdrActive && _driver is not null)
        {
            var lastPttBeforeAwaits = _lastPtt;
            var canWriteWebsdr = MonotonicClock.NowS() - _lastWebsdrWriteAt >= WebsdrMinWriteGapS;

            if (state.Ptt is not null && state.Ptt != _lastPtt && canWriteWebsdr)
            {
                _lastPtt = state.Ptt;
                if (state.Ptt.Value)
                {
                    if (_settings.MuteOnTx)
                    {
                        await _driver.SetMutedAsync(true);
                        _lastWebsdrWriteAt = MonotonicClock.NowS();
                    }
                }
                else
                {
                    // Always unmute on the falling edge, regardless of the
                    // current MuteOnTx value -- otherwise unchecking it
                    // mid-transmission could leave the WebSDR muted forever.
                    await _driver.SetMutedAsync(false);
                    _lastWebsdrWriteAt = MonotonicClock.NowS();
                }
            }

            var transmitting = _lastPtt ?? false;

            if (!transmitting)
            {
                var now = MonotonicClock.NowS();
                var forwardGeneration = _forwardLatchGeneration;
                var dueForPeriodicResync = now - _lastFullResyncAt >= FullResyncIntervalS;
                var resyncPushAttempted = false;
                var resyncPushBlocked = false;
                var forwardSuperseded = false;

                (string Mode, int? PassbandHz)? modeKey = state.Mode is not null ? (state.Mode, state.PassbandHz) : null;

                if (!_forwardSyncPaused && modeKey is not null && (!Equals(modeKey, _lastSentModeKey) || dueForPeriodicResync) && _modePush is null)
                {
                    var backoff = _forwardModeBackoff;
                    backoff.NoteTarget(modeKey);
                    if (canWriteWebsdr && MonotonicClock.NowS() >= backoff.NextAttemptAt)
                    {
                        var applied = await _driver.SetModeAsync(state.Mode!, state.PassbandHz);
                        _lastWebsdrWriteAt = MonotonicClock.NowS();
                        resyncPushAttempted = true;
                        if (forwardGeneration != _forwardLatchGeneration)
                        {
                            forwardSuperseded = true;
                        }
                        else if (applied)
                        {
                            backoff.RecordSuccess();
                            _lastSentModeKey = modeKey;
                            // A mode change can change the effective
                            // frequency sent to the WebSDR (e.g. CW
                            // offset), so force a re-push even if the raw
                            // rig frequency itself didn't move.
                            _lastSentFreq = null;
                            _lastPushedToWebsdrMode = state.Mode;
                            _forwardPushCompletedAt = MonotonicClock.NowS();
                            _reverseReseedDue = true;
                        }
                        else
                        {
                            backoff.RecordFailure(MonotonicClock.NowS());
                        }
                    }
                    else if (!canWriteWebsdr)
                    {
                        resyncPushBlocked = true;
                    }
                }

                if (state.FreqHz is not null && !forwardSuperseded)
                {
                    if (_pendingFreq is null || Math.Abs(state.FreqHz.Value - _pendingFreq.Value) > FreqChangeThresholdHz)
                    {
                        _pendingFreq = state.FreqHz;
                        _pendingFreqSince = now;
                    }
                    else if (!_forwardSyncPaused
                             && now - _pendingFreqSince >= FreqDebounceS
                             && (_lastSentFreq is null || Math.Abs(_pendingFreq.Value - _lastSentFreq.Value) > FreqChangeThresholdHz || dueForPeriodicResync)
                             && _freqPush is null)
                    {
                        var freqChanged = _lastSentFreq is null || Math.Abs(_pendingFreq.Value - _lastSentFreq.Value) > FreqChangeThresholdHz;
                        var verify = freqChanged || _modePush is null;
                        var backoff = _forwardFreqBackoff;
                        backoff.NoteTarget(_pendingFreq);
                        if (canWriteWebsdr && MonotonicClock.NowS() >= backoff.NextAttemptAt)
                        {
                            var applied = await _driver.TuneHzAsync(_pendingFreq.Value, verify);
                            _lastWebsdrWriteAt = MonotonicClock.NowS();
                            resyncPushAttempted = true;
                            if (forwardGeneration != _forwardLatchGeneration)
                            {
                                forwardSuperseded = true;
                            }
                            else if (applied)
                            {
                                backoff.RecordSuccess();
                                _lastSentFreq = _pendingFreq;
                                _lastPushedToWebsdrFreq = _pendingFreq;
                                _forwardPushCompletedAt = MonotonicClock.NowS();
                                _reverseReseedDue = true;
                            }
                            else
                            {
                                backoff.RecordFailure(MonotonicClock.NowS());
                            }
                        }
                        else if (!canWriteWebsdr)
                        {
                            resyncPushBlocked = true;
                        }
                    }
                }

                if (dueForPeriodicResync && resyncPushAttempted && !resyncPushBlocked && !forwardSuperseded)
                {
                    _lastFullResyncAt = now;
                }
            }

            websdrStatus = await _driver.GetStatusAsync();

            var reversePtt = state.Ptt ?? lastPttBeforeAwaits;
            if (!(reversePtt ?? false)
                && websdrStatus.Connected
                && !_reverseSyncHeld
                && MonotonicClock.NowS() - _forwardPushCompletedAt >= ReverseHoldoffS)
            {
                await ReverseSyncTickAsync(websdrStatus, state);
            }
        }

        if (!_websdrActive)
        {
            websdrStatus = null;
        }

        Publish(rigConnected: true, rigFreqHz: state.FreqHz, rigMode: state.Mode, rigPtt: state.Ptt, websdr: websdrStatus);
    }

    internal static string FormatDuration(double seconds) =>
        seconds < 90 ? $"{seconds:F0}s" : $"{seconds / 60:F0} min";

    internal static string DescribeWebviewCreateError(Exception e) => $"Could not create the WebSDR browser view: {e.Message}";
}
