using System.Net.Sockets;
using Microsoft.Extensions.Logging;
using SDRSync.Core;

namespace SDRSync.Rig;

/// <summary>
/// XML-RPC client for flrig's remote-control interface (not rigctld/hamlib),
/// ported from sdrsync/rig/flrig.py.
///
/// Mostly read-only: GetFreq/GetMode/GetBandwidth/GetPtt/GetState drive the
/// rig -> WebSDR forward sync. SetFreq/SetMode additionally support the
/// reverse direction (WebSDR -> rig) via flrig's rig.set_vfoA/rig.set_mode
/// RPCs. Never add SetPtt -- PTT is explicitly excluded from reverse sync
/// (an unauthenticated web page must never be able to key a real
/// transmitter; rig.set_ptt also blocks up to ~1s server-side
/// retry-polling actual PTT state on the real flrig, one more reason it's
/// excluded).
///
/// Wire formats mirror flrig's actual C++ source (not its HTML docs, which
/// disagree with the source on frequency formatting -- the docs show
/// decimals, the source sends a plain integer string). The source also
/// confirms neither rig.set_vfoA nor rig.set_mode gives a reliable success
/// signal in their RPC response, so every reverse push here is verified
/// via a bounded poll-until-match readback of the corresponding get_*()
/// call instead of trusting the call's own return value.
///
/// Unlike the Python original -- which had to dispatch every call through
/// loop.run_in_executor() because xmlrpc.client is synchronous/blocking --
/// this uses HttpClient, which is natively async, so no executor-dispatch
/// workaround is needed here. The same external behavior (a bounded
/// per-call latency) is achieved directly via a CancellationToken instead.
/// </summary>
public sealed class FlrigClient : IRigClient, IAsyncDisposable
{
    public const double ConnectTimeoutS = 2.0;
    public const double CmdTimeoutS = 1.0;
    public const double ReconnectBaseDelayS = 2.0;
    public const double ReconnectMaxDelayS = 30.0;
    public const int ReconnectWarnAfter = 5;

    // Bounded poll-until-match readback budget for SetFreq()/SetMode().
    // flrig's own get_vfo/get_mode reflect its internally cached,
    // polling-refreshed state, not live hardware state at the instant of
    // the set RPC's reply, so a naive immediate single readback would
    // report almost every successful push as a failure. Bounded by wall
    // clock, not attempt count -- see RigctldClient for the same
    // reasoning (mirrored here for consistency between the two backends).
    public const double SetVerifyBudgetS = 1.5;
    public const double SetVerifyPollIntervalS = 0.15;
    public const int FreqVerifyToleranceHz = 10;

    public string Host { get; }
    public int Port { get; }

    private readonly double _connectTimeoutS;
    private readonly double _cmdTimeoutS;
    private readonly double _reconnectBaseDelayS;
    private readonly double _reconnectMaxDelayS;
    private readonly Uri _endpoint;

    private HttpClient? _http;
    private int _reconnectFailures;

    public FlrigClient(
        string host,
        int port,
        double? connectTimeoutS = null,
        double? cmdTimeoutS = null,
        double? reconnectBaseDelayS = null,
        double? reconnectMaxDelayS = null)
    {
        Host = host;
        Port = port;
        _connectTimeoutS = connectTimeoutS ?? ConnectTimeoutS;
        _cmdTimeoutS = cmdTimeoutS ?? CmdTimeoutS;
        _reconnectBaseDelayS = reconnectBaseDelayS ?? ReconnectBaseDelayS;
        _reconnectMaxDelayS = reconnectMaxDelayS ?? ReconnectMaxDelayS;
        _endpoint = new Uri($"http://{host}:{port}/RPC2");
    }

    public bool Connected => _http is not null;

    public Task CloseAsync()
    {
        _http?.Dispose();
        _http = null;
        return Task.CompletedTask;
    }

    /// <summary>
    /// main.get_version is flrig's one precondition-free call -- it
    /// doesn't depend on a rig being attached, unlike get_vfo/get_mode/
    /// get_bw/get_ptt (which fall back to safe placeholder values instead
    /// of erroring on the real server). Probing with it here catches a
    /// broken connection immediately rather than on the first real poll call.
    /// </summary>
    public async Task<bool> ConnectAsync()
    {
        await CloseAsync();
        var http = new HttpClient { Timeout = TimeSpan.FromSeconds(_cmdTimeoutS) };
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(_connectTimeoutS));
            await CallRawAsync(http, _endpoint, "main.get_version", Array.Empty<object?>(), cts.Token);
        }
        catch (Exception e) when (IsRpcFailure(e))
        {
            http.Dispose();
            return false;
        }

        _http = http;
        return true;
    }

    public double ReconnectDelay() =>
        Math.Min(_reconnectBaseDelayS * Math.Pow(2, _reconnectFailures), _reconnectMaxDelayS);

    public async Task<bool> EnsureConnectedAsync()
    {
        if (Connected) return true;
        var ok = await ConnectAsync();
        if (ok)
        {
            if (_reconnectFailures > 0)
            {
                CoreLog.Logger.LogInformation("Reconnected to flrig after {Failures} failed attempt(s)", _reconnectFailures);
            }

            _reconnectFailures = 0;
            return true;
        }

        _reconnectFailures++;
        if (_reconnectFailures % ReconnectWarnAfter == 0)
        {
            CoreLog.Logger.LogWarning(
                "{Failures} consecutive failed attempts to connect to flrig at {Host}:{Port}",
                _reconnectFailures, Host, Port);
        }
        else
        {
            CoreLog.Logger.LogDebug("flrig connection attempt {Failures} failed", _reconnectFailures);
        }

        return false;
    }

    /// <summary>
    /// Dispatch one RPC call, with a hard timeout. Null on any failure --
    /// also drops the connection, since a failure proves the underlying
    /// HttpClient (and whatever connection it has pooled) is no longer
    /// trustworthy; the next EnsureConnectedAsync() rebuilds a fresh one.
    /// </summary>
    private async Task<object?> CallAsync(string method, IReadOnlyList<object?> args)
    {
        if (_http is null) return null;
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(_cmdTimeoutS));
            return await CallRawAsync(_http, _endpoint, method, args, cts.Token);
        }
        catch (Exception e) when (IsRpcFailure(e))
        {
            CoreLog.Logger.LogWarning("Lost connection to flrig ({Message})", e.Message);
            await CloseAsync();
            return null;
        }
    }

    /// <summary>
    /// Like CallAsync, but never drops the connection on a single failed
    /// attempt -- used only by SetFreq()/SetMode()'s bounded
    /// poll-until-match readback loop, where one slow/timed-out poll among
    /// several retries should not force a full reconnect.
    /// </summary>
    private async Task<object?> PollCallAsync(string method, IReadOnlyList<object?> args)
    {
        if (_http is null) return null;
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(_cmdTimeoutS));
            return await CallRawAsync(_http, _endpoint, method, args, cts.Token);
        }
        catch (Exception e) when (IsRpcFailure(e))
        {
            return null;
        }
    }

    private static async Task<object?> CallRawAsync(
        HttpClient http, Uri endpoint, string method, IReadOnlyList<object?> args, CancellationToken ct)
    {
        var requestXml = XmlRpcCodec.EncodeRequest(method, args);
        using var content = new StringContent(requestXml, System.Text.Encoding.UTF8, "text/xml");
        using var response = await http.PostAsync(endpoint, content, ct);
        response.EnsureSuccessStatusCode();
        var responseXml = await response.Content.ReadAsStringAsync(ct);
        return XmlRpcCodec.DecodeResponse(responseXml);
    }

    private static bool IsRpcFailure(Exception e) =>
        e is HttpRequestException or OperationCanceledException or XmlRpcCodec.XmlRpcFaultException
            or System.Xml.XmlException or FormatException or SocketException;

    public async Task<int?> GetFreqAsync()
    {
        if (_http is null) return null;
        var resp = await CallAsync("rig.get_vfo", Array.Empty<object?>());
        return ParseFreqResponse(resp);
    }

    /// <summary>Just the mode string -- unlike rigctld, flrig's rig.get_mode and rig.get_bw are independent RPCs.</summary>
    public async Task<string?> GetModeAsync()
    {
        if (_http is null) return null;
        var resp = await CallAsync("rig.get_mode", Array.Empty<object?>());
        return ParseModeResponse(resp);
    }

    public async Task<int?> GetBandwidthAsync()
    {
        if (_http is null) return null;
        var resp = await CallAsync("rig.get_bw", Array.Empty<object?>());
        return ParseBandwidthResponse(resp);
    }

    public async Task<bool?> GetPttAsync()
    {
        if (_http is null) return null;
        var resp = await CallAsync("rig.get_ptt", Array.Empty<object?>());
        var ptt = ParsePttResponse(resp);
        if (ptt is null && resp is not null)
        {
            CoreLog.Logger.LogWarning("Unexpected PTT value from flrig: {Resp}", resp);
        }

        return ptt;
    }

    /// <summary>
    /// Reverse-sync (WebSDR -> rig). set_vfoA's RPC response is not a
    /// reliable success signal, so this verifies via a bounded
    /// poll-until-match readback of GetFreqAsync() instead.
    ///
    /// Sends freqHz as a double, not an int: real flrig registers
    /// rig.set_vfoA with XML-RPC signature "d:d" (confirmed against
    /// flrig's own C++ source, src/server/xml_server.cxx, which does
    /// `(double)params[0]`), and its bundled XmlRpc++ library enforces
    /// that signature strictly by wire type -- sending a plain &lt;i4&gt;
    /// serializes as the wrong type and real flrig rejects it outright
    /// with Fault -1 "type error", before ever reaching the rig.
    /// </summary>
    public async Task<bool> SetFreqAsync(int freqHz, double? verifyBudgetS = null)
    {
        if (_http is null) return false;
        var budget = verifyBudgetS ?? SetVerifyBudgetS;
        await CallAsync("rig.set_vfoA", new object?[] { (double)freqHz });

        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(budget);
        while (true)
        {
            await Task.Delay(TimeSpan.FromSeconds(SetVerifyPollIntervalS));
            if (_http is null) break;
            var resp = await PollCallAsync("rig.get_vfo", Array.Empty<object?>());
            var actual = ParseFreqResponse(resp);
            if (actual is not null && Math.Abs(actual.Value - freqHz) <= FreqVerifyToleranceHz)
            {
                return true;
            }

            if (DateTime.UtcNow >= deadline)
            {
                break;
            }
        }

        CoreLog.Logger.LogWarning("flrig did not confirm set_vfoA({FreqHz}) within {Budget:F2}s", freqHz, budget);
        return false;
    }

    /// <summary>
    /// Reverse-sync (WebSDR -> rig). passbandHz is accepted for interface
    /// parity with RigctldClient.SetModeAsync but unused -- flrig's
    /// rig.set_mode takes only a mode name.
    /// </summary>
    public async Task<bool> SetModeAsync(string modeName, int? passbandHz, double? verifyBudgetS = null)
    {
        if (_http is null) return false;
        var budget = verifyBudgetS ?? SetVerifyBudgetS;
        await CallAsync("rig.set_mode", new object?[] { modeName });

        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(budget);
        while (true)
        {
            await Task.Delay(TimeSpan.FromSeconds(SetVerifyPollIntervalS));
            if (_http is null) break;
            var resp = await PollCallAsync("rig.get_mode", Array.Empty<object?>());
            var actual = ParseModeResponse(resp);
            if (actual is not null && actual == modeName)
            {
                return true;
            }

            if (DateTime.UtcNow >= deadline)
            {
                break;
            }
        }

        CoreLog.Logger.LogWarning("flrig did not confirm set_mode({ModeName}) within {Budget:F2}s", modeName, budget);
        return false;
    }

    /// <summary>
    /// Convenience: fetch freq/mode/bandwidth/ptt in one call -- 4
    /// sequential XML-RPC round-trips (vs. rigctld's 3), each
    /// independently null-tolerant.
    /// </summary>
    public async Task<RigState> GetStateAsync()
    {
        var freq = await GetFreqAsync();
        var mode = await GetModeAsync();
        var passband = await GetBandwidthAsync();
        var ptt = await GetPttAsync();
        return new RigState(freq, mode, passband, ptt);
    }

    public ValueTask DisposeAsync() => new(CloseAsync());

    // --- Pure parsers -------------------------------------------------

    /// <summary>Pure parser for rig.get_vfo's response, e.g. "14070000" (plain integer Hz string, no decimal point).</summary>
    public static int? ParseFreqResponse(object? resp)
    {
        if (resp is not string s) return null;
        var trimmed = s.Trim();
        var digitsPart = trimmed.TrimStart('-');
        if (digitsPart.Length > 0 && digitsPart.All(char.IsAsciiDigit)
            && int.TryParse(trimmed, System.Globalization.NumberStyles.Integer, System.Globalization.CultureInfo.InvariantCulture, out var v))
        {
            return v;
        }

        return null;
    }

    /// <summary>Pure parser for rig.get_mode's response, e.g. "USB".</summary>
    public static string? ParseModeResponse(object? resp)
    {
        if (resp is not string s) return null;
        var trimmed = s.Trim();
        return trimmed.Length > 0 ? trimmed : null;
    }

    /// <summary>
    /// Pure parser for rig.get_bw's response: a 2-element array of
    /// strings. For most rigs element 0 holds the value and element 1 is
    /// ""; for rigs with dual DSP lo/hi controls, both elements hold
    /// separate lo/hi values. Only element 0 is used.
    /// </summary>
    public static int? ParseBandwidthResponse(object? resp)
    {
        if (resp is not object?[] arr || arr.Length < 1) return null;
        if (arr[0] is not string value) return null;
        var trimmed = value.Trim();
        var digitsPart = trimmed.TrimStart('-');
        if (digitsPart.Length > 0 && digitsPart.All(char.IsAsciiDigit)
            && int.TryParse(trimmed, System.Globalization.NumberStyles.Integer, System.Globalization.CultureInfo.InvariantCulture, out var v))
        {
            return v;
        }

        return null;
    }

    /// <summary>
    /// Pure parser for rig.get_ptt's response: a plain XML-RPC int 0/1.
    /// An XML-RPC &lt;boolean&gt; decodes to a C# bool, a structurally
    /// distinct type from int (unlike Python, where bool is an int
    /// subclass and needs an explicit isinstance exclusion) -- so `resp is
    /// int` alone already rejects a genuine boolean value here.
    /// </summary>
    public static bool? ParsePttResponse(object? resp)
    {
        if (resp is not int i) return null;
        return i != 0;
    }
}
