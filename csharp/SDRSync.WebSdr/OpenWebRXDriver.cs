using System.Text.Json;
using Microsoft.Extensions.Logging;
using SDRSync.Core;

namespace SDRSync.WebSdr;

/// <summary>
/// Driver for the OpenWebRX software family (including the OpenWebRX+ fork
/// -- same client JS, same driver), ported 1:1 from
/// sdrsync/websdr/openwebrx.py.
///
/// Strategy: same as the other drivers -- drive the real page's own
/// control objects (confirmed by reading the live site's actual bundled JS
/// and calling them live in a real browser tab against a real receiver)
/// via IWebSdrPage.EvaluateAsync, rather than reimplementing the control
/// WebSocket's JSON protocol.
///
/// Confirmed live (against http://sdr2.justjakob.de/, cross-checked for
/// the fingerprint/structure against two other instances on different
/// versions):
///     $('#openwebrx-panel-receiver').demodulatorPanel().getDemodulator()
///         -- the one-and-only active demodulator object. Confirmed
///         .set_offset_frequency(offset_hz) retunes the receiver and
///         .offset_frequency reads it back; demodulatorPanel().setMode(mode)
///         changes mode and .getDemodulator().modulation reads it back.
///     Frequency is OFFSET-RELATIVE, not absolute: offset_hz is relative to
///         the top-level global window.center_freq, and the page itself
///         no-ops a set_offset_frequency() call if abs(offset_hz) exceeds
///         window.bandwidth / 2 (some stations run multiple SDR profiles
///         with different center/bandwidth; switching profiles to cover an
///         out-of-range request is not implemented -- see TuneHzAsync).
///     window.ws -- the control WebSocket; ws.readyState === 1 (OPEN) is
///         the reliable "ready" signal, same semantics as KiwiSDR's ws_snd.
///     Valid modulation strings come from the server at runtime
///         (Modes.getModes()), not hardcoded client JS -- confirmed live:
///         nfm/wfm/am/lsb/usb/cw are the analog ones. Unlike the other two
///         drivers there is NO narrow/wide suffix convention here, so
///         MapHamlibModeOpenwebrx() is a plain 1:1 dict.
///     toggleMute() -- confirmed live: toggles a UI class on
///         '.openwebrx-mute-button' and drives audioEngine.setVolume();
///         reused directly (rather than reimplemented) so SetMutedAsync()
///         doesn't need to track/restore the pre-mute volume itself --
///         checking '.openwebrx-mute-button' has class 'muted' before
///         deciding whether to call it makes SetMutedAsync() idempotent.
///     The demodulator does not exist/start immediately when `ws` opens
///         (the page's own init sequence creates it slightly later, gated
///         on Modes.initComplete() && center_freq) -- confirmed live via
///         getDemodulators()[0].started flipping true only after that.
///         AttachAsync() must wait for this, the same class of startup
///         race already hit and fixed for KiwiSDR (there:
///         demodulators.length).
/// </summary>
public sealed class OpenWebRXDriver : IWebSDRDriver
{
    public static readonly string[] FingerprintMarkers = { "compiled/receiver.js" };

    private const double LoadTimeoutS = 15.0;
    private const int FreqVerifyToleranceHz = 10;

    // hamlib mode name -> OpenWebRX modulation string. Confirmed live via
    // Modes.getModes() that these carry no narrow/wide suffix convention
    // (unlike websdr.org's ...N suffix or KiwiSDR's usn/lsn/cwn/etc.), so
    // this is a plain 1:1 dict -- not an oversight that passbandHz isn't
    // used here.
    private static readonly Dictionary<string, string> ModeMap = new()
    {
        ["USB"] = "usb",
        ["PKTUSB"] = "usb",
        ["DATA-U"] = "usb",
        ["LSB"] = "lsb",
        ["PKTLSB"] = "lsb",
        ["DATA-L"] = "lsb",
        ["CW"] = "cw",
        ["CWR"] = "cw",
        ["CW-U"] = "cw",
        ["CW-L"] = "cw",
        ["AM"] = "am",
        ["FM"] = "nfm",
        ["WFM"] = "wfm",
    };

    /// <summary>Pure mapping from a hamlib mode name to an OpenWebRX modulation string. Returns null if there's no known mapping. No passband parameter, unlike MapHamlibMode/MapHamlibModeKiwi -- see class doc comment.</summary>
    public static string? MapHamlibModeOpenwebrx(string hamlibMode) => ModeMap.GetValueOrDefault(hamlibMode.ToUpperInvariant());

    /// <summary>Human-readable kHz formatting for error messages -- same convention as kiwisdr.py and the GUI, instead of a raw Hz integer, which is hard to read at a glance for HF/VHF frequencies.</summary>
    private static string FmtKhz(double? hz) => hz is null ? "unknown" : $"{hz / 1000:F3} kHz";

    // JS readiness predicate for AttachAsync's WaitForFunctionAsync -- checks
    // the EXACT access path every control call below uses
    // ($('#openwebrx-panel-receiver').demodulatorPanel().getDemodulator()),
    // not just that a sibling global function exists, so a page that passes
    // this check is actually ready for TuneHzAsync/SetModeAsync to succeed.
    // Wrapped in try/catch so a predicate that throws mid-page-init fails
    // the wait cleanly.
    private const string ReadyPredicate = """
        () => {
            try {
                if (typeof $ !== 'function' || !window.ws || window.ws.readyState !== 1) return false;
                var panel = $('#openwebrx-panel-receiver').demodulatorPanel();
                if (!panel) return false;
                var demod = panel.getDemodulator();
                return !!demod && demod.started === true
                    && Number.isFinite(window.center_freq)
                    && Number.isFinite(window.bandwidth) && window.bandwidth > 0;
            } catch (e) {
                return false;
            }
        }
        """;

    // get_status()'s already-normalized mode string (mode_str.ToUpper(),
    // the exact post-normalization set -- USB/LSB/CW/AM/NFM/WFM) ->
    // canonical hamlib mode name. Unlike the other two drivers there's no
    // narrow/wide suffix convention on this site at all, so this reverse
    // map is a plain 1:1 dict too.
    private static readonly Dictionary<string, string> ReverseModeMap = new()
    {
        ["USB"] = "USB",
        ["LSB"] = "LSB",
        ["CW"] = "CW",
        ["AM"] = "AM",
        ["NFM"] = "FM",
        ["WFM"] = "WFM",
    };

    /// <summary>Pure reverse mapping: OpenWebRX's get_status()-normalized mode string (already uppercased) -> a canonical hamlib mode name. Null if unknown.</summary>
    public static string? MapOpenwebrxModeToHamlib(string? openwebrxMode) =>
        openwebrxMode is null ? null : ReverseModeMap.GetValueOrDefault(openwebrxMode.ToUpperInvariant());

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

    // Cached from the most recent GetStatusAsync() (which the engine calls
    // every tick, always before TuneHzAsync/SetModeAsync within the same
    // tick), used to (a) skip a page round-trip entirely for a frequency
    // already known to be out of the current profile's range instead of
    // retrying it every 200ms forever, and (b) detect the profile's
    // center/bandwidth changing underneath the driver (e.g. someone
    // switches profile in the browser tab).
    internal int? _centerFreq;
    internal int? _bandwidth;
    private (int, int?, int?)? _lastOutOfRangeKey;
    private string? _lastUnmappedMode;

    public OpenWebRXDriver(string url, int cwOffsetHz = 0)
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
            // A reattach can be triggered by GetStatusAsync() noticing the
            // SDR profile's center/bandwidth changed (see below) -- that
            // means THIS page is already loaded and may already be ready,
            // e.g. because the operator manually switched profile in the
            // browser tab. Re-navigating unconditionally would silently
            // discard that choice back to the server's default profile, so
            // check readiness in place first and only navigate if that's
            // not already satisfied (a fresh, never-navigated page safely
            // evaluates to "not ready" here rather than erroring).
            var alreadyReady = await page.EvaluateAsync(ReadyPredicate);
            if (alreadyReady is not { ValueKind: JsonValueKind.True })
            {
                await page.NavigateAsync(Url, LoadTimeoutS);
                await page.WaitForFunctionAsync(ReadyPredicate, LoadTimeoutS);
            }
        }
        catch (BrowserException e)
        {
            _lastAttachError =
                $"Page at {Url} did not behave like a compatible OpenWebRX within {LoadTimeoutS}s " +
                $"(demodulator/control WebSocket not found ready): {e.Message}";
            throw new WebSDRIncompatibleException(_lastAttachError);
        }

        _attached = true;
        _currentMode = null;
        _centerFreq = null;
        _bandwidth = null;
        _lastOutOfRangeKey = null;
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
        // inline (below), not a delayed background task.
        if (!_attached) return false;
        var effectiveHz = freqHz;
        if (_currentMode == "cw") effectiveHz += CwOffsetHz;

        // Cheap local pre-check using the values the last GetStatusAsync()
        // saw -- avoids a page round-trip (and a fresh warning log line)
        // for a frequency that's already known to be outside the active
        // profile's range and hasn't changed, which would otherwise happen
        // every poll tick for as long as the rig sits there.
        if (_centerFreq is not null && _bandwidth is not null && Math.Abs(effectiveHz - _centerFreq.Value) > _bandwidth.Value / 2.0)
        {
            var key = (effectiveHz, _centerFreq, _bandwidth);
            if (!key.Equals(_lastOutOfRangeKey))
            {
                _lastOutOfRangeKey = key;
                _lastTuneError = OutOfRangeMessage(effectiveHz);
                CoreLog.Logger.LogWarning("{Message}", _lastTuneError);
            }
            else
            {
                CoreLog.Logger.LogDebug("Frequency still out of range (unchanged): {Freq}", effectiveHz);
            }

            return false;
        }

        JsonElement? result;
        try
        {
            result = await _page!.EvaluateAsync(
                "(freq_hz) => { " +
                "if (!window.ws || window.ws.readyState !== 1) return {status: 'not_ready'}; " +
                "var cf = window.center_freq, bw = window.bandwidth; " +
                "if (!Number.isFinite(cf) || !Number.isFinite(bw) || bw <= 0) return {status: 'not_ready'}; " +
                "var offset = Math.round(freq_hz - cf); " +
                "if (Math.abs(offset) > bw / 2) return {status: 'out_of_range', center_freq: cf, bandwidth: bw}; " +
                "var demod = $('#openwebrx-panel-receiver').demodulatorPanel().getDemodulator(); " +
                "demod.set_offset_frequency(offset); " +
                "return {status: 'ok', offset: demod.offset_frequency, center_freq: cf, bandwidth: bw}; " +
                "}",
                effectiveHz);
        }
        catch (BrowserException e)
        {
            _lastTuneError = $"set_offset_frequency() failed: {e.Message}";
            CoreLog.Logger.LogWarning("{Message}", _lastTuneError);
            return false;
        }

        var status = result is { ValueKind: JsonValueKind.Object } o && o.TryGetProperty("status", out var st) ? st.GetString() : null;
        if (status == "not_ready")
        {
            _lastTuneError = "OpenWebRX control WebSocket is not open (disconnected mid-session?)";
            CoreLog.Logger.LogWarning("{Message}", _lastTuneError);
            _attached = false;
            return false;
        }

        var (resultCenterFreq, resultBandwidth) = ExtractCenterBandwidth(result);
        UpdateProfileCache(resultCenterFreq, resultBandwidth);

        if (status == "out_of_range")
        {
            var key = (effectiveHz, resultCenterFreq, resultBandwidth);
            _lastTuneError = OutOfRangeMessage(effectiveHz);
            if (!key.Equals(_lastOutOfRangeKey))
            {
                CoreLog.Logger.LogWarning("{Message}", _lastTuneError);
            }
            else
            {
                CoreLog.Logger.LogDebug("Frequency still out of range (unchanged): {Freq}", effectiveHz);
            }

            _lastOutOfRangeKey = key;
            return false;
        }

        var actualOffset = result is { ValueKind: JsonValueKind.Object } ro && ro.TryGetProperty("offset", out var off) && off.ValueKind == JsonValueKind.Number
            ? off.GetDouble()
            : (double?)null;
        if (actualOffset is null || !double.IsFinite(actualOffset.Value))
        {
            _lastTuneError = "Could not read back offset_frequency to verify set_offset_frequency()";
            CoreLog.Logger.LogWarning("{Message}", _lastTuneError);
            return false;
        }

        var actualHz = _centerFreq is not null ? _centerFreq.Value + actualOffset.Value : (double?)null;
        if (actualHz is null || Math.Abs(actualHz.Value - effectiveHz) > FreqVerifyToleranceHz)
        {
            _lastTuneError = $"OpenWebRX did not apply requested frequency: wanted {FmtKhz(effectiveHz)}, reads {FmtKhz(actualHz)}";
            CoreLog.Logger.LogWarning("{Message}", _lastTuneError);
            return false;
        }

        _lastTuneError = null;
        _lastOutOfRangeKey = null;
        return true;
    }

    private static (int?, int?) ExtractCenterBandwidth(JsonElement? result)
    {
        if (result is not { ValueKind: JsonValueKind.Object } o) return (null, null);
        int? cf = o.TryGetProperty("center_freq", out var c) && c.ValueKind == JsonValueKind.Number ? (int)c.GetDouble() : null;
        int? bw = o.TryGetProperty("bandwidth", out var b) && b.ValueKind == JsonValueKind.Number ? (int)b.GetDouble() : null;
        return (cf, bw);
    }

    private string OutOfRangeMessage(int effectiveHz)
    {
        if (_centerFreq is null || _bandwidth is null)
        {
            return $"Requested frequency {FmtKhz(effectiveHz)} is outside the active SDR profile's range";
        }

        var lo = _centerFreq.Value - _bandwidth.Value / 2;
        var hi = _centerFreq.Value + _bandwidth.Value / 2;
        return $"Requested frequency {FmtKhz(effectiveHz)} is outside the active SDR profile's range " +
               $"({FmtKhz(lo)} - {FmtKhz(hi)}); switching profiles automatically isn't supported yet";
    }

    private void UpdateProfileCache(int? centerFreq, int? bandwidth)
    {
        if (centerFreq is not null) _centerFreq = centerFreq;
        if (bandwidth is > 0) _bandwidth = bandwidth;
    }

    public async Task<bool> SetModeAsync(string hamlibMode, int? passbandHz)
    {
        // passbandHz is accepted for IWebSDRDriver interface compatibility
        // but unused -- OpenWebRX's modulation strings have no narrow/wide
        // variant (see MapHamlibModeOpenwebrx).
        if (!_attached) return false;
        var mode = MapHamlibModeOpenwebrx(hamlibMode);
        if (mode is null)
        {
            _lastModeError = $"hamlib mode '{hamlibMode}' has no OpenWebRX equivalent; frequency sync continues";
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
                "if (!window.ws || window.ws.readyState !== 1) return 'not_ready'; " +
                "$('#openwebrx-panel-receiver').demodulatorPanel().setMode(mode); " +
                "return 'ok'; " +
                "}",
                mode);
        }
        catch (BrowserException e)
        {
            _lastModeError = $"setMode() failed: {e.Message}";
            CoreLog.Logger.LogWarning("{Message}", _lastModeError);
            return false;
        }

        if (result is { ValueKind: JsonValueKind.String } s && s.GetString() == "not_ready")
        {
            _lastModeError = "OpenWebRX control WebSocket is not open (disconnected mid-session?)";
            CoreLog.Logger.LogWarning("{Message}", _lastModeError);
            _attached = false;
            return false;
        }

        var actualMode = await ReadModeAsync();
        if (actualMode != mode)
        {
            _lastModeError = $"OpenWebRX did not apply requested mode: wanted '{mode}', reads '{actualMode}'";
            CoreLog.Logger.LogWarning("{Message}", _lastModeError);
            return false;
        }

        _currentMode = mode;
        _lastModeError = null;
        _lastUnmappedMode = null;
        return true;
    }

    private async Task<string?> ReadModeAsync()
    {
        try
        {
            var result = await _page!.EvaluateAsync(
                "() => { var d = $('#openwebrx-panel-receiver').demodulatorPanel().getDemodulator(); " +
                "return d ? d.modulation : null; }");
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
                "(wantMuted) => { " +
                "var isMuted = $('.openwebrx-mute-button').hasClass('muted'); " +
                "if (wantMuted !== isMuted && typeof toggleMute === 'function') toggleMute(); " +
                "}",
                muted);
        }
        catch (BrowserException e)
        {
            CoreLog.Logger.LogDebug("toggleMute() call failed (non-fatal): {Message}", e.Message);
        }
    }

    /// <summary>Un-applies CwOffsetHz for the reverse direction (WebSDR -> rig), symmetric to TuneHzAsync's forward application. See WebsdrOrgDriver's identical helper for the full reasoning on why this reads the passed-in mode, not _currentMode.</summary>
    private int? ReverseEffectiveHz(int? observedHz, string? observedHamlibMode) =>
        observedHz is null ? null : observedHamlibMode == "CW" ? observedHz - CwOffsetHz : observedHz;

    public string? HamlibModeFromStatus(WebSDRStatus status) => MapOpenwebrxModeToHamlib(status.Mode);

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
                "() => { " +
                "var demod = $('#openwebrx-panel-receiver').demodulatorPanel().getDemodulator(); " +
                "return {center_freq: window.center_freq, bandwidth: window.bandwidth, " +
                "offset: demod ? demod.offset_frequency : null, " +
                "mode: demod ? demod.modulation : null, " +
                "ws_ready: window.ws ? window.ws.readyState === 1 : false}; " +
                "}");

            double? centerFreq = null, bandwidth = null, offset = null;
            string? modeStr = null;
            bool wsReady = false;
            if (data is { ValueKind: JsonValueKind.Object } obj)
            {
                if (obj.TryGetProperty("center_freq", out var cf) && cf.ValueKind == JsonValueKind.Number) centerFreq = cf.GetDouble();
                if (obj.TryGetProperty("bandwidth", out var bw) && bw.ValueKind == JsonValueKind.Number) bandwidth = bw.GetDouble();
                if (obj.TryGetProperty("offset", out var off) && off.ValueKind == JsonValueKind.Number) offset = off.GetDouble();
                if (obj.TryGetProperty("mode", out var m) && m.ValueKind == JsonValueKind.String) modeStr = m.GetString();
                if (obj.TryGetProperty("ws_ready", out var wr) && wr.ValueKind is JsonValueKind.True or JsonValueKind.False) wsReady = wr.GetBoolean();
            }

            // A dropped control WebSocket is otherwise invisible: the
            // page's globals stay readable (this evaluate keeps
            // succeeding), and once the rig settles on a frequency the
            // engine stops calling TuneHzAsync/SetModeAsync altogether
            // (their own readyState re-checks -- the actual recovery
            // trigger -- would then never run), leaving the driver
            // silently wedged "connected" until the rig's value next changes.
            if (!wsReady)
            {
                _attached = false;
                _lastPageError = "OpenWebRX control WebSocket is not open (disconnected mid-session?)";
                CoreLog.Logger.LogWarning("{Message}", _lastPageError);
                return new WebSDRStatus(Connected: false, LastError: CombinedError());
            }

            // Each field is checked for finiteness independently before any
            // arithmetic -- a transient non-finite reading here must not
            // raise (which would otherwise flip _attached off on a
            // perfectly healthy page, the same bug class already hit once
            // for KiwiSDR's frequency readback).
            var centerFreqOk = centerFreq is not null && double.IsFinite(centerFreq.Value);
            var bandwidthOk = bandwidth is > 0 && double.IsFinite(bandwidth.Value);
            var offsetOk = offset is not null && double.IsFinite(offset.Value);

            // The active SDR profile's center/bandwidth changing underneath
            // the driver (someone switches profile in the browser tab, or
            // the server pushes a new one) would otherwise leave the
            // demodulator's stale offset silently pointing at the wrong
            // absolute frequency forever, since the rig's own frequency
            // hasn't moved so nothing would re-trigger a push. Detaching
            // here routes recovery through the existing attach-supervisor
            // path, which resets the sync latches and forces a fresh push
            // at the new center once re-attached.
            if (centerFreqOk && bandwidthOk && _centerFreq is not null && _bandwidth is not null
                && ((int)centerFreq!.Value != _centerFreq.Value || (int)bandwidth!.Value != _bandwidth.Value))
            {
                _attached = false;
                _lastPageError =
                    $"OpenWebRX SDR profile changed underneath the driver (center/bandwidth {_centerFreq}/{_bandwidth} " +
                    $"-> {(int)centerFreq!.Value}/{(int)bandwidth!.Value}); reattaching";
                CoreLog.Logger.LogWarning("{Message}", _lastPageError);
                return new WebSDRStatus(Connected: false, LastError: CombinedError());
            }

            if (centerFreqOk && bandwidthOk) UpdateProfileCache((int)centerFreq!.Value, (int)bandwidth!.Value);

            var freqHz = centerFreqOk && offsetOk ? (int?)((int)centerFreq!.Value + (int)offset!.Value) : null;

            return new WebSDRStatus(
                Connected: true,
                FreqHz: freqHz,
                Mode: modeStr?.ToUpperInvariant(),
                // Proxy: "is the control socket open", not a true audio-is-
                // actually-playing signal, same documented caveat as
                // KiwiSDR's proxy.
                AudioActive: wsReady,
                LastError: CombinedError());
        }
        catch (BrowserException e)
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
            CoreLog.Logger.LogWarning("[OpenWebRX page console:{Type}] {Text}", msg.Type, msg.Text);
        }
    }

    private void OnPageError(Exception exc)
    {
        _lastPageError = $"Unhandled JS error on OpenWebRX page: {exc.Message}";
        CoreLog.Logger.LogError("{Message}", _lastPageError);
    }
}
