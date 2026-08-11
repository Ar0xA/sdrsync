using System.Text.Json;
using Microsoft.Extensions.Logging;
using SDRSync.Core;

namespace SDRSync.WebSdr;

/// <summary>
/// Driver for the KiwiSDR software family (e.g. *.proxy.kiwisdr.com
/// instances), ported 1:1 from sdrsync/websdr/kiwisdr.py.
///
/// Strategy: same as the websdr.org driver -- drive the real page's own
/// control functions (confirmed by reading the live site's actual JS
/// source and calling them live in a real browser tab) via
/// IWebSdrPage.EvaluateAsync, rather than reimplementing KiwiSDR's
/// websocket wire protocol.
///
/// Confirmed global functions/objects on the page:
///     ext_tune(freq_dial_kHz, mode, zoom, zlevel, low_cut, high_cut, opt)
///         -- omitted args are left as JS `undefined` and treated as "don't
///         change this": ext_tune(khz) alone retunes frequency only (mode
///         unaffected); ext_tune(undefined, mode) alone changes mode only
///         (frequency unaffected). Confirmed live against a real instance.
///     ext_get_freq_kHz()  -- returns a STRING like "14074.00", not a number.
///     ext_get_mode()      -- returns the current mode as a lowercase string.
///     ws_snd              -- the sound WebSocket; ws_snd.readyState === 1
///                             (OPEN) is the reliable "ready" signal.
///     toggle_or_set_mute(1|0) -- mutes/unmutes local playback volume.
///
/// Unlike PA3FWM, a KiwiSDR instance is a single continuous-range receiver --
/// there is no setband()-equivalent and no band table. That does NOT mean
/// tune/mode requests can't silently fail though: public instances can drop
/// ws_snd out from under you (user-count/time limits), and admin-configured
/// frequency ranges can silently clamp/reject a request -- so TuneHzAsync/
/// SetModeAsync both check ws_snd.readyState and verify via a readback, the
/// same "only report success if it actually happened" discipline as
/// WebsdrOrgDriver.
/// </summary>
public sealed class KiwiSDRDriver : IWebSDRDriver
{
    public static readonly string[] FingerprintMarkers = { "kiwisdr.min.js" };

    private const double LoadTimeoutS = 15.0;
    private const int FreqVerifyToleranceHz = 10;
    private const int NarrowThresholdHz = 2000;
    private const int WideAmThresholdHz = 8000;

    // hamlib mode name -> (kiwi base mode, kiwi narrow-variant mode or null
    // if this mode has no narrow variant). Narrow-variant strings are taken
    // verbatim from the live site's passbands_fallback table -- most do NOT
    // follow a simple "+ n" suffix rule (usb -> usn, lsb -> lsn), so they're
    // listed explicitly rather than derived.
    private static readonly Dictionary<string, (string BaseMode, string? NarrowMode)> ModeMap = new()
    {
        ["USB"] = ("usb", "usn"),
        ["PKTUSB"] = ("usb", "usn"),
        ["DATA-U"] = ("usb", "usn"),
        ["LSB"] = ("lsb", "lsn"),
        ["PKTLSB"] = ("lsb", "lsn"),
        ["DATA-L"] = ("lsb", "lsn"),
        ["CW"] = ("cw", "cwn"),
        ["CWR"] = ("cw", "cwn"),
        ["CW-U"] = ("cw", "cwn"),
        ["CW-L"] = ("cw", "cwn"),
        ["AM"] = ("am", "amn"),
        ["SAM"] = ("sam", null),
        ["FM"] = ("nbfm", "nnfm"),
        ["WFM"] = ("nbfm", "nnfm"),
    };

    /// <summary>Pure mapping from a hamlib mode name (+ optional passband) to a KiwiSDR mode string. Returns null if there's no known mapping.</summary>
    public static string? MapHamlibModeKiwi(string hamlibMode, int? passbandHz)
    {
        if (!ModeMap.TryGetValue(hamlibMode.ToUpperInvariant(), out var mapped)) return null;
        var (baseMode, narrowMode) = mapped;
        if (narrowMode is null || passbandHz is null or <= 0) return baseMode;
        if (baseMode == "am")
        {
            if (passbandHz < NarrowThresholdHz) return narrowMode;
            if (passbandHz > WideAmThresholdHz) return "amw";
            return "am";
        }

        return passbandHz < NarrowThresholdHz ? narrowMode : baseMode;
    }

    /// <summary>
    /// Strip a KiwiSDR mode string down to its base ('usn'/'cwn'/'amn'/'nnfm'
    /// -> 'usb'/'cw'/'am'/'nbfm'), for comparisons that must not care about
    /// the narrow/wide variant (e.g. "is the rig in CW so the CW offset
    /// applies"). Falls back to returning the string unchanged if it's not
    /// a known variant (covers 'amw', 'sam', and any mode this driver
    /// doesn't map to).
    /// </summary>
    internal static string BaseModeOf(string kiwiMode)
    {
        foreach (var (baseMode, narrowMode) in ModeMap.Values)
        {
            if (kiwiMode == narrowMode) return baseMode;
        }

        return kiwiMode;
    }

    // get_status()'s already-normalized mode string (BaseModeOf(...).ToUpper(),
    // the exact post-normalization set -- USB/LSB/CW/AM/AMW/SAM/NBFM) ->
    // canonical hamlib mode name. NOT mechanically derived from ModeMap
    // above (many-to-one there); since the input here is already
    // base-collapsed and uppercased, BaseModeOf() would be a no-op if
    // re-applied, so this maps directly from the confirmed uppercase set.
    private static readonly Dictionary<string, string> ReverseModeMap = new()
    {
        ["USB"] = "USB",
        ["LSB"] = "LSB",
        ["CW"] = "CW",
        ["AM"] = "AM",
        ["AMW"] = "AM", // no separate hamlib wide-AM mode exists
        ["SAM"] = "SAM",
        ["NBFM"] = "FM",
    };

    /// <summary>Pure reverse mapping: KiwiSDR's get_status()-normalized mode string (already base-collapsed and uppercased) -> a canonical hamlib mode name. Null if unknown.</summary>
    public static string? MapKiwiModeToHamlib(string? kiwiMode) =>
        kiwiMode is null ? null : ReverseModeMap.GetValueOrDefault(kiwiMode.ToUpperInvariant());

    public string Url { get; }
    internal int CwOffsetHz { get; }

    internal IWebSdrPage? _page;
    internal string? _currentMode;
    internal bool _attached;
    private bool _listenersRegistered;
    private string? _lastAttachError;
    private string? _lastPageError;
    internal string? _lastTuneError;
    private string? _lastModeError;
    private string? _lastUnmappedMode;

    public KiwiSDRDriver(string url, int cwOffsetHz = 0)
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

        try
        {
            await page.NavigateAsync(Url, LoadTimeoutS);
            await page.WaitForFunctionAsync(
                "() => typeof window.ext_tune === 'function' " +
                "&& typeof window.ext_get_mode === 'function' " +
                "&& typeof window.ext_get_freq_kHz === 'function' " +
                "&& window.ws_snd && window.ws_snd.readyState === 1 " +
                // ws_snd opening doesn't mean the page is ready to be
                // retuned yet -- the page's own default demodulator is
                // created slightly *after* the sound socket opens (its own
                // init callback), and calling ext_tune() before that exists
                // throws inside the page's own JS (demodulators[0] is
                // undefined). Confirmed empirically: readyState hits 1 up
                // to ~1-2s before demodulators.length goes from 0 to 1.
                "&& typeof demodulators !== 'undefined' && demodulators.length > 0",
                LoadTimeoutS);
        }
        catch (BrowserException e)
        {
            _lastAttachError =
                $"Page at {Url} did not behave like a compatible KiwiSDR within {LoadTimeoutS}s " +
                $"(control functions and/or open sound socket not found): {e.Message}";
            throw new WebSDRIncompatibleException(_lastAttachError);
        }

        _attached = true;
        _currentMode = null;
        _lastAttachError = null;
        _lastPageError = null;
        _lastTuneError = null;
        _lastModeError = null;
        _lastUnmappedMode = null;
    }

    public async Task<bool> TuneHzAsync(int freqHz, bool verify = true)
    {
        // verify is accepted for IWebSDRDriver interface parity but unused
        // here -- this driver's readback verification is synchronous and
        // inline (below), not a delayed background task, so there's
        // nothing for verify=false to skip.
        if (!_attached) return false;
        var effectiveHz = freqHz;
        if (_currentMode is not null && BaseModeOf(_currentMode) == "cw") effectiveHz += CwOffsetHz;

        JsonElement? result;
        try
        {
            result = await _page!.EvaluateAsync(
                "(khz) => { " +
                "if (!window.ws_snd || window.ws_snd.readyState !== 1) return 'not_ready'; " +
                "window.ext_tune(khz); " +
                "return 'ok'; " +
                "}",
                effectiveHz / 1000.0);
        }
        catch (BrowserException e)
        {
            _lastTuneError = $"ext_tune() failed: {e.Message}";
            CoreLog.Logger.LogWarning("{Message}", _lastTuneError);
            return false;
        }

        if (result is { ValueKind: JsonValueKind.String } s && s.GetString() == "not_ready")
        {
            _lastTuneError = "KiwiSDR sound socket is not open (disconnected mid-session?)";
            CoreLog.Logger.LogWarning("{Message}", _lastTuneError);
            _attached = false;
            return false;
        }

        var actualHz = await ReadFreqHzAsync();
        if (actualHz is null)
        {
            _lastTuneError = "Could not read back frequency to verify ext_tune()";
            CoreLog.Logger.LogWarning("{Message}", _lastTuneError);
            return false;
        }

        if (Math.Abs(actualHz.Value - effectiveHz) > FreqVerifyToleranceHz)
        {
            _lastTuneError =
                $"KiwiSDR did not apply requested frequency: wanted {effectiveHz / 1000.0:F3} kHz, " +
                $"reads {actualHz.Value / 1000.0:F3} kHz (out of the receiver's configured range?)";
            CoreLog.Logger.LogWarning("{Message}", _lastTuneError);
            return false;
        }

        _lastTuneError = null;
        return true;
    }

    private async Task<int?> ReadFreqHzAsync()
    {
        try
        {
            var khzStr = await _page!.EvaluateAsync("() => window.ext_get_freq_kHz()");
            if (khzStr is not { ValueKind: JsonValueKind.String } s) return null;
            return (int)Math.Round(double.Parse(s.GetString()!, System.Globalization.CultureInfo.InvariantCulture) * 1000);
        }
        catch (Exception e) when (e is BrowserException or FormatException)
        {
            return null;
        }
    }

    public async Task<bool> SetModeAsync(string hamlibMode, int? passbandHz)
    {
        if (!_attached) return false;
        var kiwiMode = MapHamlibModeKiwi(hamlibMode, passbandHz);
        if (kiwiMode is null)
        {
            _lastModeError = $"hamlib mode '{hamlibMode}' has no KiwiSDR equivalent; frequency sync continues";
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

        JsonElement? result;
        try
        {
            result = await _page!.EvaluateAsync(
                "(mode) => { " +
                "if (!window.ws_snd || window.ws_snd.readyState !== 1) return 'not_ready'; " +
                "window.ext_tune(undefined, mode); " +
                "return 'ok'; " +
                "}",
                kiwiMode);
        }
        catch (BrowserException e)
        {
            _lastModeError = $"ext_tune(mode) failed: {e.Message}";
            CoreLog.Logger.LogWarning("{Message}", _lastModeError);
            return false;
        }

        if (result is { ValueKind: JsonValueKind.String } s && s.GetString() == "not_ready")
        {
            _lastModeError = "KiwiSDR sound socket is not open (disconnected mid-session?)";
            CoreLog.Logger.LogWarning("{Message}", _lastModeError);
            _attached = false;
            return false;
        }

        var actualMode = await ReadModeAsync();
        if (actualMode != kiwiMode)
        {
            _lastModeError = $"KiwiSDR did not apply requested mode: wanted '{kiwiMode}', reads '{actualMode}'";
            CoreLog.Logger.LogWarning("{Message}", _lastModeError);
            return false;
        }

        _currentMode = kiwiMode;
        _lastModeError = null;
        _lastUnmappedMode = null;
        return true;
    }

    private async Task<string?> ReadModeAsync()
    {
        try
        {
            var result = await _page!.EvaluateAsync("() => window.ext_get_mode()");
            return result is { ValueKind: JsonValueKind.String } s ? s.GetString() : null;
        }
        catch (BrowserException)
        {
            return null;
        }
    }

    public async Task SetMutedAsync(bool muted)
    {
        if (!_attached) return;
        try
        {
            await _page!.EvaluateAsync(
                "(m) => { if (window.toggle_or_set_mute) window.toggle_or_set_mute(m ? 1 : 0); }", muted);
        }
        catch (BrowserException e)
        {
            CoreLog.Logger.LogDebug("toggle_or_set_mute() call failed (non-fatal): {Message}", e.Message);
        }
    }

    /// <summary>
    /// Un-applies CwOffsetHz for the reverse direction (WebSDR -> rig),
    /// symmetric to TuneHzAsync's forward application above. Takes the mode
    /// as an explicit argument, sourced from the SAME status snapshot
    /// MapKiwiModeToHamlib() derived it from -- NOT _currentMode, which is
    /// stale by construction for reverse sync (see WebsdrOrgDriver's
    /// identical helper for the full reasoning).
    /// </summary>
    private int? ReverseEffectiveHz(int? observedHz, string? observedHamlibMode) =>
        observedHz is null ? null : observedHamlibMode == "CW" ? observedHz - CwOffsetHz : observedHz;

    public string? HamlibModeFromStatus(WebSDRStatus status) => MapKiwiModeToHamlib(status.Mode);

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
                "() => ({freq: window.ext_get_freq_kHz ? window.ext_get_freq_kHz() : null, " +
                "mode: window.ext_get_mode ? window.ext_get_mode() : null, " +
                "audio: window.ws_snd ? window.ws_snd.readyState === 1 : null})");

            string? freqStr = null;
            string? modeStr = null;
            bool? audio = null;
            if (data is { ValueKind: JsonValueKind.Object } obj)
            {
                if (obj.TryGetProperty("freq", out var f) && f.ValueKind == JsonValueKind.String) freqStr = f.GetString();
                if (obj.TryGetProperty("mode", out var m) && m.ValueKind == JsonValueKind.String) modeStr = m.GetString();
                if (obj.TryGetProperty("audio", out var a) && a.ValueKind is JsonValueKind.True or JsonValueKind.False) audio = a.GetBoolean();
            }

            // ext_get_freq_kHz() can legitimately return "nan" (e.g. right
            // after attach, before any tune has been sent yet) -- that's
            // "frequency not known yet", not a fatal error, so it must not
            // raise out of this try block and flip _attached off.
            int? freqHz = null;
            if (freqStr is not null)
            {
                var freqKhz = double.Parse(freqStr, System.Globalization.CultureInfo.InvariantCulture);
                if (!double.IsNaN(freqKhz)) freqHz = (int)Math.Round(freqKhz * 1000);
            }

            return new WebSDRStatus(
                Connected: true,
                FreqHz: freqHz,
                // Displayed mode is normalized to its base (non-narrow)
                // form -- ext_get_mode() returns KiwiSDR's own internal
                // string verbatim (e.g. "usn"/"lsn" for a narrow-filter
                // USB/LSB selection), which reads as a different mode than
                // the hamlib USB/LSB it actually corresponds to if shown raw.
                Mode: modeStr is not null ? BaseModeOf(modeStr).ToUpperInvariant() : null,
                // Proxy: "is the sound socket open", not a true audio-is-
                // actually-playing signal -- no confirmed AudioContext
                // global for KiwiSDR yet.
                AudioActive: audio,
                LastError: CombinedError());
        }
        catch (Exception e) when (e is BrowserException or FormatException)
        {
            _attached = false;
            _lastPageError = e.Message;
            return new WebSDRStatus(Connected: false, LastError: CombinedError());
        }
    }

    public Task CloseAsync()
    {
        _attached = false;
        _page = null;
        return Task.CompletedTask;
    }

    private void OnConsole(ConsoleMessage msg)
    {
        if (msg.Type is "error" or "warning")
        {
            CoreLog.Logger.LogWarning("[KiwiSDR page console:{Type}] {Text}", msg.Type, msg.Text);
        }
    }

    private void OnPageError(Exception exc)
    {
        _lastPageError = $"Unhandled JS error on KiwiSDR page: {exc.Message}";
        CoreLog.Logger.LogError("{Message}", _lastPageError);
    }
}
