using System.Text.Json;
using Microsoft.Extensions.Logging;
using SDRSync.Core;

namespace SDRSync.WebSdr;

/// <summary>
/// Driver for the classic "WebSDR" software from websdr.org (by PA3FWM;
/// e.g. websdr.ewi.utwente.nl), ported 1:1 from sdrsync/websdr/websdr_org.py.
///
/// Strategy: rather than reimplementing the site's custom binary
/// websocket/audio protocol, drive the real page's own control functions
/// (confirmed by reading the live site's actual JS source) via
/// IWebSdrPage.EvaluateAsync. This gets genuine audio playback for free
/// through the real browser tab, and is resilient in the sense that any
/// control-API break is fail-loud (WebSDRIncompatibleException) rather
/// than a silent protocol mismatch.
///
/// Confirmed global functions on the page:
///     setfreq(khz: float)     -- set receive frequency within the current band
///     set_mode(mode: str)     -- "USB"/"USBN"/"LSB"/"LSBN"/"AM"/"AMN"/"FM"/"CW"/"AMSYNC"
///     setband(idx: int)       -- switch which (possibly non-overlapping) band is active
///     soundapplet.mute(1|0)   -- true mute/unmute of the WebSocket audio stream
///     freq (global var)       -- the current carrier/BFO frequency in kHz, as
///                                last stored by setfreq(); this is what the page
///                                sends upstream ("f=" + freq.toFixed(3)), so it
///                                carries full 1 Hz resolution. Note the VISIBLE
///                                frequency box (document.freqform.frequency) is
///                                only nomfreq.toFixed(2), i.e. rounded to 10 Hz --
///                                a sub-10 Hz retune is real but invisible there.
///                                There is no #freqinput element on any websdr.org
///                                build (verified live against five instances).
///
/// The Twente instance itself has a single band (nbands=1) spanning ~0-29 MHz,
/// so band switching is a no-op there in practice, but the logic is generic so
/// other/future websdr.org instances with multiple bands work unmodified.
/// </summary>
public sealed class WebsdrOrgDriver : IWebSDRDriver
{
    public static readonly string[] FingerprintMarkers = { "websdr-base.js" };

    private const double LoadTimeoutS = 15.0;
    private const double FreqVerifyDelayS = 0.6;
    private const int FreqVerifyToleranceHz = 10;
    private const double AudioGateCheckDelayS = 1.0;
    private const int NarrowUsbLsbThresholdHz = 2000;
    private const int NarrowAmThresholdHz = 6000;

    // hamlib mode name -> (webSDR base mode, supports an "N" narrow variant)
    private static readonly Dictionary<string, (string BaseMode, bool SupportsNarrow)> ModeMap = new()
    {
        ["USB"] = ("USB", true),
        ["PKTUSB"] = ("USB", true),
        ["DATA-U"] = ("USB", true),
        ["LSB"] = ("LSB", true),
        ["PKTLSB"] = ("LSB", true),
        ["DATA-L"] = ("LSB", true),
        ["CW"] = ("CW", false),
        ["CWR"] = ("CW", false),
        ["CW-U"] = ("CW", false),
        ["CW-L"] = ("CW", false),
        ["AM"] = ("AM", true),
        ["SAM"] = ("AMSYNC", false),
        ["FM"] = ("FM", false),
        ["WFM"] = ("FM", false),
    };

    /// <summary>Pure mapping from a hamlib mode name (+ optional passband) to a WebSDR mode string. Returns null if there's no known mapping (caller should skip mode sync and log, not throw).</summary>
    public static string? MapHamlibMode(string hamlibMode, int? passbandHz)
    {
        if (!ModeMap.TryGetValue(hamlibMode.ToUpperInvariant(), out var mapped)) return null;
        var (baseMode, supportsNarrow) = mapped;
        if (supportsNarrow && passbandHz is > 0)
        {
            var threshold = baseMode == "AM" ? NarrowAmThresholdHz : NarrowUsbLsbThresholdHz;
            if (passbandHz < threshold) return baseMode + "N";
        }

        return baseMode;
    }

    // WebSDR base mode string (post-N-stripping) -> canonical hamlib mode
    // name. Reverse of ModeMap above, but NOT mechanically derived from it:
    // ModeMap is many-to-one (e.g. USB/PKTUSB/DATA-U all forward-map to
    // "USB"), so each collision here is an explicit, deliberate choice of
    // ONE canonical hamlib name to reverse-map back to.
    private static readonly Dictionary<string, string> ReverseModeMap = new()
    {
        ["USB"] = "USB",
        ["LSB"] = "LSB",
        ["CW"] = "CW",
        ["AM"] = "AM",
        ["AMSYNC"] = "SAM",
        ["FM"] = "FM",
    };

    /// <summary>Pure reverse mapping: a websdr.org base mode string (already narrow-stripped) -> a canonical hamlib mode name. Null if unknown.</summary>
    public static string? MapWebsdrModeToHamlib(string? websdrMode) =>
        websdrMode is null ? null : ReverseModeMap.GetValueOrDefault(websdrMode.ToUpperInvariant());

    public string Url { get; }
    internal int CwOffsetHz { get; }

    internal IWebSdrPage? _page;
    internal List<(double Low, double High)> _bands = new();
    internal int? _currentBand;
    internal string? _currentMode;
    internal bool _attached;
    private bool _listenersRegistered;
    internal string? _lastTuneError;
    private string? _lastModeError;
    private string? _lastPageError;
    private string? _lastAttachError;
    internal Task? _verifyTask;
    private CancellationTokenSource? _verifyCts;
    private string? _lastUnmappedMode;
    private int? _lastOutOfRangeHz;

    public WebsdrOrgDriver(string url, int cwOffsetHz = 0)
    {
        Url = url;
        CwOffsetHz = cwOffsetHz;
    }

    public bool Attached => _attached;

    public async Task AttachAsync(IWebSdrPage page)
    {
        _page = page;
        if (!_listenersRegistered)
        {
            page.OnConsole(OnConsole);
            page.OnPageError(OnPageError);
            _listenersRegistered = true;
        }

        // A (re)navigation resets the page's JS back to default, so our
        // idea of "current band/mode" from before this attach attempt is
        // stale and must not survive it -- otherwise TuneHzAsync would
        // skip a needed setband() call after recovering from an outage,
        // silently tuning the wrong band.
        _currentBand = null;
        _currentMode = null;

        try
        {
            await page.NavigateAsync(Url, LoadTimeoutS);
            await page.WaitForFunctionAsync(
                "() => typeof window.setfreq === 'function' && typeof window.set_mode === 'function' " +
                "&& typeof window.setband === 'function'",
                LoadTimeoutS);
            await LoadBandTableAsync();
            await SatisfyAudioGateAsync();
        }
        catch (WebSDRIncompatibleException e)
        {
            _lastAttachError = e.Message;
            throw;
        }
        catch (Exception e) when (e is BrowserException or JsonException or FormatException or InvalidOperationException)
        {
            _lastAttachError = $"Page at {Url} did not behave like a compatible websdr.org WebSDR within {LoadTimeoutS}s: {e}";
            throw new WebSDRIncompatibleException(_lastAttachError);
        }

        _attached = true;
        // A fresh, successful attach means any error recorded before or
        // during the outage is no longer current -- clear all of them, not
        // just the attach-specific one, or a stale JS/tune error would keep
        // showing in the GUI forever after a full recovery.
        _lastAttachError = null;
        _lastPageError = null;
        _lastTuneError = null;
        _lastModeError = null;
        _lastUnmappedMode = null;
        _lastOutOfRangeHz = null;
    }

    private async Task LoadBandTableAsync()
    {
        JsonElement? raw;
        try
        {
            raw = await _page!.EvaluateAsync("() => bandinfo.map(b => ({c: b.centerfreq, s: b.samplerate}))");
        }
        catch (BrowserException e)
        {
            throw new WebSDRIncompatibleException($"Could not read bandinfo from page: {e.Message}");
        }

        _bands = new List<(double, double)>();
        if (raw is { ValueKind: JsonValueKind.Array } array)
        {
            foreach (var entry in array.EnumerateArray())
            {
                var centerHz = entry.GetProperty("c").GetDouble() * 1000.0;
                var spanHz = entry.GetProperty("s").GetDouble() * 1000.0;
                _bands.Add((centerHz - spanHz / 2, centerHz + spanHz / 2));
            }
        }

        if (_bands.Count == 0) throw new WebSDRIncompatibleException("Page's bandinfo array was empty");
        CoreLog.Logger.LogInformation("Loaded {Count} band(s) from {Url}", _bands.Count, Url);
    }

    /// <summary>
    /// Best-effort: satisfy the browser's autoplay-gesture requirement.
    /// Clicks a corner of the viewport rather than the body's (default)
    /// center point, since on this site the body center is typically the
    /// tuning waterfall/spectrum canvas -- clicking there is click-to-tune
    /// and would retune the receiver. A corner click is very unlikely to
    /// land on any control.
    /// </summary>
    private async Task SatisfyAudioGateAsync()
    {
        try
        {
            await _page!.ClickAsync(2, 2);
        }
        catch (BrowserException)
        {
        }

        await Task.Delay(TimeSpan.FromSeconds(AudioGateCheckDelayS));
        try
        {
            var state = await _page!.EvaluateAsync("() => (document.ct ? document.ct.state : null)");
            if (state is { ValueKind: JsonValueKind.String } s && s.GetString() == "suspended")
            {
                await _page.EvaluateAsync("() => document.ct && document.ct.resume()");
                CoreLog.Logger.LogWarning("Audio context was suspended; requested resume()");
            }
        }
        catch (BrowserException e)
        {
            CoreLog.Logger.LogDebug("Could not inspect/resume audio context: {Message}", e.Message);
        }
    }

    internal int? BandForFreq(int freqHz)
    {
        for (var idx = 0; idx < _bands.Count; idx++)
        {
            var (low, high) = _bands[idx];
            if (low <= freqHz && freqHz <= high) return idx;
        }

        return null;
    }

    public async Task<bool> TuneHzAsync(int freqHz, bool verify = true)
    {
        if (!_attached) return false;
        var effectiveHz = freqHz;
        if (_currentMode == "CW") effectiveHz += CwOffsetHz;

        var bandIdx = BandForFreq(effectiveHz);
        if (bandIdx is null)
        {
            _lastTuneError = $"{effectiveHz / 1000.0:F3} kHz is outside all bands this WebSDR covers";
            // Rate-limited: WARNING the first time a given frequency is
            // rejected, DEBUG on exact repeats -- the engine retries a
            // rejected forward push on its own schedule, so a rig parked
            // on an out-of-band frequency would otherwise emit an
            // identical WARNING forever.
            if (effectiveHz != _lastOutOfRangeHz)
            {
                CoreLog.Logger.LogWarning("{Message}", _lastTuneError);
            }
            else
            {
                CoreLog.Logger.LogDebug("Frequency still outside all bands (unchanged): {Freq}", effectiveHz);
            }

            _lastOutOfRangeHz = effectiveHz;
            return false;
        }

        try
        {
            if (bandIdx != _currentBand)
            {
                await _page!.EvaluateAsync("(idx) => window.setband(idx)", bandIdx);
                _currentBand = bandIdx;
            }

            await _page!.EvaluateAsync("(khz) => window.setfreq(khz)", effectiveHz / 1000.0);
            _lastTuneError = null;
        }
        catch (BrowserException e)
        {
            _lastTuneError = $"setfreq() failed: {e.Message}";
            CoreLog.Logger.LogWarning("{Message}", _lastTuneError);
            return false;
        }

        if (verify) ScheduleFreqVerification(effectiveHz);
        return true;
    }

    private void ScheduleFreqVerification(int expectedHz)
    {
        _verifyCts?.Cancel();
        var cts = new CancellationTokenSource();
        _verifyCts = cts;
        _verifyTask = VerifyFreqAppliedAsync(expectedHz, cts.Token);
    }

    /// <summary>
    /// Reads back the frequency the page itself currently holds. Reads the
    /// page's own global `window.freq` (kHz), NOT a text input. The
    /// previous implementation read `#freqinput`, an element that does not
    /// exist on any websdr.org build (verified live against five real
    /// instances: Twente, Hack Green, IS0GRB, NA5B, Maasbree -- the
    /// visible frequency box is `document.freqform.frequency`, which
    /// carries a `name` and no `id`). So every verification silently
    /// returned null and logged "Could not read back #freqinput to verify
    /// setfreq()", leaving VerifyFreqAppliedAsync() permanently dead
    /// (hundreds of such warnings in a real user's log).
    ///
    /// `window.freq` is the right source rather than that text box for two
    /// independent reasons, both read off websdr-base.js:
    ///   * the box is written as `nomfreq.toFixed(2)` -- rounded to 10 Hz,
    ///     so it cannot confirm a sub-10 Hz tune at all; and
    ///   * in CW it shows nominalfreq() (carrier + (hi+lo)/2), whereas
    ///     TuneHzAsync() passes the carrier/BFO frequency as expectedHz --
    ///     comparing against the box would be off by the CW filter centre
    ///     (~750 Hz with the site's default CW filter) and would trip the
    ///     mismatch retry on every single CW tune.
    /// `freq` is exactly the value setfreq() stores and exactly what gets
    /// sent upstream as "f=" + freq.toFixed(3), so it is both unrounded
    /// and mode-independent.
    /// </summary>
    internal async Task<int?> ReadPageFreqHzAsync()
    {
        var value = await _page!.EvaluateAsync(
            "() => (typeof window.freq === 'number' && isFinite(window.freq)) ? window.freq : null");
        if (value is not { ValueKind: JsonValueKind.Number } n) return null;
        return (int)Math.Round(n.GetDouble() * 1000);
    }

    internal async Task VerifyFreqAppliedAsync(int expectedHz, CancellationToken ct)
    {
        try
        {
            await Task.Delay(TimeSpan.FromSeconds(FreqVerifyDelayS), ct);
            if (!_attached) return;
            var actualHz = await ReadPageFreqHzAsync();
            if (actualHz is null)
            {
                CoreLog.Logger.LogWarning("Could not read back window.freq to verify setfreq()");
                return;
            }

            if (Math.Abs(actualHz.Value - expectedHz) <= FreqVerifyToleranceHz) return;

            CoreLog.Logger.LogWarning(
                "WebSDR did not apply requested frequency: wanted {Wanted:F3} kHz, page shows {Actual:F3} kHz. Retrying band selection once.",
                expectedHz / 1000.0, actualHz.Value / 1000.0);
            // One retry: force band re-selection in case setfreq was
            // silently ignored because we were at a band edge.
            var bandIdx = BandForFreq(expectedHz);
            if (bandIdx is null)
            {
                _lastTuneError = $"WebSDR frequency mismatch and {expectedHz / 1000.0:F3} kHz is outside all known bands";
                return;
            }

            await _page!.EvaluateAsync("(idx) => window.setband(idx)", bandIdx);
            _currentBand = bandIdx;
            await _page.EvaluateAsync("(khz) => window.setfreq(khz)", expectedHz / 1000.0);

            await Task.Delay(TimeSpan.FromSeconds(FreqVerifyDelayS), ct);
            actualHz = await ReadPageFreqHzAsync();
            _lastTuneError = actualHz is not null && Math.Abs(actualHz.Value - expectedHz) <= FreqVerifyToleranceHz
                ? null
                : "WebSDR frequency mismatch persists after retry; see log";
        }
        catch (OperationCanceledException)
        {
        }
        catch (BrowserException e)
        {
            CoreLog.Logger.LogDebug("Frequency verification failed (non-fatal): {Message}", e.Message);
        }
    }

    public async Task<bool> SetModeAsync(string hamlibMode, int? passbandHz)
    {
        if (!_attached) return false;
        var webMode = MapHamlibMode(hamlibMode, passbandHz);
        if (webMode is null)
        {
            _lastModeError = $"hamlib mode '{hamlibMode}' has no WebSDR equivalent; frequency sync continues";
            if (hamlibMode != _lastUnmappedMode)
            {
                _lastUnmappedMode = hamlibMode;
                CoreLog.Logger.LogWarning("{Message}", _lastModeError);
            }
            else
            {
                CoreLog.Logger.LogDebug("{Message}", _lastModeError);
            }

            return false;
        }

        try
        {
            await _page!.EvaluateAsync("(m) => window.set_mode(m)", webMode);
            // Base mode without the "N" (narrow) suffix, used e.g. to
            // detect CW for CwOffsetHz.
            _currentMode = webMode.EndsWith("N") ? webMode[..^1] : webMode;
            _lastModeError = null;
            _lastUnmappedMode = null;
            return true;
        }
        catch (BrowserException e)
        {
            _lastModeError = $"set_mode() failed: {e.Message}";
            CoreLog.Logger.LogWarning("{Message}", _lastModeError);
            return false;
        }
    }

    public async Task SetMutedAsync(bool muted)
    {
        if (!_attached) return;
        try
        {
            await _page!.EvaluateAsync(
                "(m) => { if (window.soundapplet) window.soundapplet.mute(m ? 1 : 0); }", muted);
        }
        catch (BrowserException e)
        {
            CoreLog.Logger.LogDebug("mute() call failed (non-fatal): {Message}", e.Message);
        }
    }

    /// <summary>
    /// Un-applies CwOffsetHz for the reverse direction (WebSDR -> rig),
    /// symmetric to TuneHzAsync's forward application above. Takes the
    /// mode as an explicit argument, sourced from the SAME status snapshot
    /// HamlibModeFromStatus() derived it from -- NOT _currentMode, which
    /// only reflects this driver's own last PUSHED mode and is stale by
    /// construction for reverse sync (a page change the driver didn't
    /// itself push is exactly what reverse sync exists to observe).
    /// </summary>
    private int? ReverseEffectiveHz(int? observedHz, string? observedHamlibMode) =>
        observedHz is null ? null : observedHamlibMode == "CW" ? observedHz - CwOffsetHz : observedHz;

    public string? HamlibModeFromStatus(WebSDRStatus status) => MapWebsdrModeToHamlib(status.Mode);

    public int? RigFreqFromStatus(WebSDRStatus status) => ReverseEffectiveHz(status.FreqHz, HamlibModeFromStatus(status));

    private string? CombinedError()
    {
        var parts = new[] { _lastAttachError, _lastPageError, _lastTuneError, _lastModeError }
            .Where(e => !string.IsNullOrEmpty(e));
        var joined = string.Join(" | ", parts);
        return joined.Length == 0 ? null : joined;
    }

    public async Task<WebSDRStatus> GetStatusAsync()
    {
        if (!_attached) return new WebSDRStatus(Connected: false, LastError: CombinedError());
        try
        {
            var data = await _page!.EvaluateAsync(
                "() => ({freq: window.freq, mode: window.mode, " +
                "audio: document.ct ? document.ct.state === 'running' : null})");
            var freqProp = data is { ValueKind: JsonValueKind.Object } o && o.TryGetProperty("freq", out var f) && f.ValueKind != JsonValueKind.Null
                ? f.GetDouble()
                : (double?)null;
            string? rawMode = null;
            bool? audio = null;
            if (data is { ValueKind: JsonValueKind.Object } obj)
            {
                if (obj.TryGetProperty("mode", out var m) && m.ValueKind == JsonValueKind.String) rawMode = m.GetString();
                if (obj.TryGetProperty("audio", out var a) && a.ValueKind is JsonValueKind.True or JsonValueKind.False) audio = a.GetBoolean();
            }

            // Displayed mode is normalized to its base (non-narrow) form --
            // window.mode can legitimately be e.g. "USBN" for a
            // narrow-filter USB selection, which reads as a different mode
            // than the hamlib USB it actually corresponds to if shown raw
            // (mirrors SetModeAsync's own _currentMode stripping above).
            var mode = rawMode is not null && rawMode.EndsWith("N") ? rawMode[..^1] : rawMode;
            return new WebSDRStatus(
                Connected: true,
                FreqHz: freqProp is not null ? (int)Math.Round(freqProp.Value * 1000) : null,
                Mode: mode,
                AudioActive: audio,
                LastError: CombinedError());
        }
        catch (BrowserException e)
        {
            // The page died out from under us -- drop back to "not
            // attached" so TuneHzAsync/SetModeAsync/SetMutedAsync become
            // no-ops again until a fresh AttachAsync (retried by the
            // engine) succeeds.
            _attached = false;
            _lastPageError = e.Message;
            return new WebSDRStatus(Connected: false, LastError: CombinedError());
        }
    }

    public async Task CloseAsync()
    {
        if (_verifyTask is not null && !_verifyTask.IsCompleted)
        {
            _verifyCts?.Cancel();
            try
            {
                await _verifyTask;
            }
            catch (OperationCanceledException)
            {
            }
            catch (BrowserException)
            {
            }
        }

        _attached = false;
        _page = null;
    }

    private void OnConsole(ConsoleMessage msg)
    {
        if (msg.Type is "error" or "warning")
        {
            CoreLog.Logger.LogWarning("[WebSDR page console:{Type}] {Text}", msg.Type, msg.Text);
        }
    }

    private void OnPageError(Exception exc)
    {
        _lastPageError = $"Unhandled JS error on WebSDR page: {exc.Message}";
        CoreLog.Logger.LogError("{Message}", _lastPageError);
    }
}
