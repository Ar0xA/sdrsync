using System.Globalization;
using System.Net.Sockets;
using System.Text;
using Microsoft.Extensions.Logging;
using SDRSync.Core;

namespace SDRSync.Rig;

/// <summary>
/// Async TCP client for hamlib's rigctld (network CAT control, not
/// Omnirig), ported from sdrsync/rig/rigctld.py.
///
/// Mostly read-only: GetFreq/GetMode/GetPtt/GetState drive the rig ->
/// WebSDR forward sync. SetFreq/SetMode additionally support the reverse
/// direction (WebSDR -> rig) via rigctld's symmetric uppercase SET
/// commands ('F &lt;hz&gt;', 'M &lt;mode&gt; &lt;passband&gt;'), each replying with
/// the same single 'RPRT &lt;code&gt;' line shape as the GET side's error
/// path. Never add SetPtt -- PTT is explicitly excluded from reverse sync
/// (an unauthenticated web page must never be able to key a real
/// transmitter).
///
/// 'RPRT 0' only confirms rigctld accepted the command syntactically, not
/// that the physical rig actually took it -- CAT-bus commands can be
/// dropped or collide, especially over serial links. SetFreq/SetMode
/// therefore verify via a poll-until-match readback after sending, polling
/// repeatedly for up to the verify budget before giving up. Wall-clock
/// bounded, not attempt-count bounded -- a fixed attempt count was found
/// (in the Python original) to give up too early on real hardware that had
/// genuinely accepted the command, just slower than the budget allowed.
/// The command is sent once, never resent while polling -- resending while
/// it may still be landing risks colliding with it again on a CAT bus that
/// doesn't tolerate overlapping commands.
///
/// A verify-loop readback poll that times out closes the connection and
/// reconnects immediately, same as any other command timeout. This is
/// deliberate, not incidental: a cancelled read does not discard bytes
/// already delivered into the OS socket receive buffer, so if the "late"
/// reply eventually arrives after the timeout fires, it would sit in the
/// stream and get misattributed to whatever command is sent next,
/// permanently desyncing the request/response pairing from that point on.
/// A fresh connection guarantees no stray buffered reply survives. This
/// hazard is not Python-specific -- it applies identically to a
/// NetworkStream-based .NET client, so the same "close and reconnect after
/// a verify-poll timeout" discipline is carried over here, not simplified
/// away.
///
/// The verify loop also always sleeps once before its first readback poll
/// (never polls at t=0) -- an immediate poll fires in the same instant the
/// SET command was written, when the rig is certain not to have applied it
/// yet, and is the poll most likely to collide with that command on the bus.
/// </summary>
public sealed class RigctldClient : IRigClient, IAsyncDisposable
{
    public const double ConnectTimeoutS = 2.0;
    public const double CmdTimeoutS = 1.0;
    public const double ReconnectBaseDelayS = 2.0;
    public const double ReconnectMaxDelayS = 30.0;
    public const int ReconnectWarnAfter = 5;

    // SetFreq()/SetMode() readback-verification cadence -- see the class
    // doc comment for why this is wall-clock bounded rather than
    // attempt-count bounded. This is only a fallback default: in practice
    // the sync engine's reverse-sync ladder always overrides it via
    // verifyBudgetS.
    public const double SetVerifyBudgetS = 1.0;
    public const double SetVerifyPollIntervalS = 0.25;

    // abs(Hz) tolerance for treating a frequency readback as "confirmed"
    // -- some rigs' CAT layers round to a step size rather than echoing
    // back the exact requested Hz.
    public const int FreqVerifyToleranceHz = 10;

    // PTT values 1 (mic), 2 (data)... hamlib backends vary; all non-zero
    // means "transmitting".
    private static readonly HashSet<string> PttTxValues = new() { "1", "2", "3" };

    public string Host { get; }
    public int Port { get; }

    private readonly double _connectTimeoutS;
    private readonly double _cmdTimeoutS;
    private readonly double _reconnectBaseDelayS;
    private readonly double _reconnectMaxDelayS;

    private TcpClient? _tcpClient;
    private NetworkStream? _stream;
    private StreamReader? _reader;
    private int _reconnectFailures;

    public RigctldClient(
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
    }

    public bool Connected => _stream is not null;

    public Task CloseAsync()
    {
        try { _reader?.Dispose(); } catch { /* best-effort teardown */ }
        try { _stream?.Dispose(); } catch { /* best-effort teardown */ }
        try { _tcpClient?.Dispose(); } catch { /* best-effort teardown */ }
        _reader = null;
        _stream = null;
        _tcpClient = null;
        return Task.CompletedTask;
    }

    public async Task<bool> ConnectAsync()
    {
        await CloseAsync();
        var client = new TcpClient();
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(_connectTimeoutS));
            await client.ConnectAsync(Host, Port, cts.Token);
        }
        catch (Exception e) when (e is OperationCanceledException or SocketException)
        {
            client.Dispose();
            return false;
        }

        _tcpClient = client;
        _stream = client.GetStream();
        _reader = new StreamReader(_stream, Encoding.ASCII);
        return true;
    }

    /// <summary>Exponential backoff delay for the *next* reconnect attempt.</summary>
    public double ReconnectDelay() =>
        Math.Min(_reconnectBaseDelayS * Math.Pow(2, _reconnectFailures), _reconnectMaxDelayS);

    /// <summary>
    /// Connect if needed; returns true if a usable connection exists
    /// afterwards. Caller is responsible for sleeping ReconnectDelay()
    /// between calls when this returns false (the sync engine's poll loop
    /// already sleeps each tick, so it composes naturally).
    /// </summary>
    public async Task<bool> EnsureConnectedAsync()
    {
        if (Connected) return true;
        var ok = await ConnectAsync();
        if (ok)
        {
            if (_reconnectFailures > 0)
            {
                CoreLog.Logger.LogInformation("Reconnected to rigctld after {Failures} failed attempt(s)", _reconnectFailures);
            }

            _reconnectFailures = 0;
            return true;
        }

        _reconnectFailures++;
        if (_reconnectFailures % ReconnectWarnAfter == 0)
        {
            CoreLog.Logger.LogWarning(
                "{Failures} consecutive failed attempts to connect to rigctld at {Host}:{Port}",
                _reconnectFailures, Host, Port);
        }
        else
        {
            CoreLog.Logger.LogDebug("rigctld connection attempt {Failures} failed", _reconnectFailures);
        }

        return false;
    }

    /// <summary>
    /// Send a command, read a single response line. Empty string on EOF
    /// (mirrors Python's `line.decode().strip()` of an empty read -- not
    /// treated as a connection-dropping failure by itself, only an actual
    /// exception is). Null on I/O failure/timeout -- always closes the
    /// connection on a timeout (see the class doc comment for why a
    /// verify-loop poll timeout must not be treated differently here;
    /// that loop instead reconnects immediately afterward via
    /// ReconnectIfDroppedAsync()). The write itself carries no timeout,
    /// matching the Python original -- only the readline is bounded.
    /// </summary>
    private async Task<string?> SendRawAsync(string cmd)
    {
        if (_stream is null || _reader is null) return null;
        try
        {
            var bytes = Encoding.ASCII.GetBytes(cmd + "\n");
            await _stream.WriteAsync(bytes, CancellationToken.None);
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(_cmdTimeoutS));
            var line = await _reader.ReadLineAsync(cts.Token);
            return (line ?? "").Trim();
        }
        catch (Exception e) when (e is OperationCanceledException or IOException or ObjectDisposedException)
        {
            CoreLog.Logger.LogWarning("Lost connection to rigctld while sending {Cmd}", cmd);
            await CloseAsync();
            return null;
        }
    }

    public async Task<int?> GetFreqAsync()
    {
        var resp = await SendRawAsync("f");
        return ParseFreqResponse(resp);
    }

    /// <summary>
    /// rigctld's 'm' command normally replies with two lines: mode name,
    /// then passband in Hz. On error it replies with a single
    /// 'RPRT &lt;code&gt;' line instead -- reading a second line in that case
    /// would block until the command timeout, so that's checked for
    /// before attempting it. Unlike SendRawAsync, an RPRT-error reply here
    /// is a normal, expected response shape -- not a transport failure --
    /// so it does NOT close the connection.
    /// </summary>
    public async Task<(string Mode, int PassbandHz)?> GetModeAsync()
    {
        if (_stream is null || _reader is null) return null;
        try
        {
            await _stream.WriteAsync(Encoding.ASCII.GetBytes("m\n"), CancellationToken.None);

            string modeLine;
            using (var cts1 = new CancellationTokenSource(TimeSpan.FromSeconds(_cmdTimeoutS)))
            {
                modeLine = (await _reader.ReadLineAsync(cts1.Token)) ?? "";
            }

            if (IsRprtError(modeLine))
            {
                CoreLog.Logger.LogDebug("rigctld returned an error for 'm': {Line}", modeLine.Trim());
                return null;
            }

            string pbLine;
            using (var cts2 = new CancellationTokenSource(TimeSpan.FromSeconds(_cmdTimeoutS)))
            {
                pbLine = (await _reader.ReadLineAsync(cts2.Token)) ?? "";
            }

            return ParseModeResponse(modeLine, pbLine);
        }
        catch (Exception e) when (e is OperationCanceledException or IOException or ObjectDisposedException)
        {
            CoreLog.Logger.LogWarning("Lost connection to rigctld while sending 'm'");
            await CloseAsync();
            return null;
        }
    }

    public async Task<bool?> GetPttAsync()
    {
        var resp = await SendRawAsync("t");
        var ptt = ParsePttResponse(resp);
        if (ptt is null && resp is not null)
        {
            CoreLog.Logger.LogWarning("Unexpected PTT value from rigctld: {Resp}", resp);
        }

        return ptt;
    }

    /// <summary>
    /// Called after a verify-loop readback poll -- if that poll's timeout
    /// just closed the connection, reconnect immediately rather than
    /// leaving it closed until the next tick's EnsureConnectedAsync()
    /// notices, which would otherwise also cost the forward sync
    /// direction a full poll interval.
    /// </summary>
    private async Task ReconnectIfDroppedAsync()
    {
        if (!Connected)
        {
            await ConnectAsync();
        }
    }

    /// <summary>
    /// Reverse-sync (WebSDR -> rig): sets the rig's VFO frequency.
    /// verifyBudgetS overrides the default SetVerifyBudgetS -- used by the
    /// sync engine's reverse-sync retry ladder, which wants a short
    /// per-attempt budget and retries several times itself rather than one
    /// long wait.
    /// </summary>
    public async Task<bool> SetFreqAsync(int freqHz, double? verifyBudgetS = null)
    {
        var budget = verifyBudgetS ?? SetVerifyBudgetS;
        var resp = await SendRawAsync($"F {freqHz}");
        if (!ParseSetResponse(resp))
        {
            CoreLog.Logger.LogWarning("rigctld rejected F {FreqHz}: {Resp}", freqHz, resp);
            return false;
        }

        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(budget);
        while (true)
        {
            await Task.Delay(TimeSpan.FromSeconds(SetVerifyPollIntervalS));
            var actual = await GetFreqAsync();
            if (actual is not null && Math.Abs(actual.Value - freqHz) <= FreqVerifyToleranceHz)
            {
                return true;
            }

            if (actual is null)
            {
                await ReconnectIfDroppedAsync();
            }

            if (DateTime.UtcNow >= deadline)
            {
                break;
            }
        }

        CoreLog.Logger.LogWarning(
            "rigctld accepted F {FreqHz} but the rig never confirmed it within {Budget:F1}s", freqHz, budget);
        return false;
    }

    /// <summary>
    /// Reverse-sync (WebSDR -> rig): sets the rig's mode only. passbandHz
    /// is accepted for interface parity with FlrigClient.SetModeAsync (see
    /// that method's doc comment) but is otherwise unused -- this always
    /// sends hamlib's real -1 "leave bandwidth alone" sentinel (confirmed
    /// via hamlib's own rigctld documentation), never passbandHz's value.
    /// 0 is a real, different hamlib value ("rig default for this mode",
    /// not "leave alone"), and a live user report found ANY reverse-sync
    /// bandwidth change unwelcome, even a "sensible" one -- so a mode
    /// change here must never touch the rig's filter/passband at all.
    /// </summary>
    public async Task<bool> SetModeAsync(string modeName, int? passbandHz, double? verifyBudgetS = null)
    {
        var budget = verifyBudgetS ?? SetVerifyBudgetS;
        var resp = await SendRawAsync($"M {modeName} -1");
        if (!ParseSetResponse(resp))
        {
            CoreLog.Logger.LogWarning("rigctld rejected M {Mode} -1: {Resp}", modeName, resp);
            return false;
        }

        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(budget);
        while (true)
        {
            await Task.Delay(TimeSpan.FromSeconds(SetVerifyPollIntervalS));
            var confirmed = await GetModeAsync();
            var actualMode = confirmed?.Mode;
            if (actualMode == modeName)
            {
                return true;
            }

            if (confirmed is null)
            {
                await ReconnectIfDroppedAsync();
            }

            if (DateTime.UtcNow >= deadline)
            {
                break;
            }
        }

        CoreLog.Logger.LogWarning(
            "rigctld accepted M {Mode} -1 but the rig never confirmed it within {Budget:F1}s", modeName, budget);
        return false;
    }

    /// <summary>Convenience: fetch freq/mode/ptt in one call. Any of them may be null on failure.</summary>
    public async Task<RigState> GetStateAsync()
    {
        var freq = await GetFreqAsync();
        var modePb = await GetModeAsync();
        var ptt = await GetPttAsync();
        return new RigState(freq, modePb?.Mode, modePb?.PassbandHz, ptt);
    }

    public ValueTask DisposeAsync() => new(CloseAsync());

    // --- Pure parsers -------------------------------------------------

    /// <summary>Pure parser for rigctld's 'f' response, e.g. "14074000".</summary>
    public static int? ParseFreqResponse(string? resp)
    {
        if (resp is null) return null;
        var trimmed = resp.Trim();
        var digitsPart = trimmed.TrimStart('-');
        if (digitsPart.Length > 0 && IsAllAsciiDigits(digitsPart)
            && int.TryParse(trimmed, NumberStyles.Integer, CultureInfo.InvariantCulture, out var v))
        {
            return v;
        }

        return null;
    }

    /// <summary>Pure parser for rigctld's two-line 'm' response: mode name, then passband Hz.</summary>
    public static (string Mode, int PassbandHz)? ParseModeResponse(string modeLine, string passbandLine)
    {
        var mode = modeLine.Trim();
        var pbStr = passbandLine.Trim();
        var pbDigits = pbStr.TrimStart('-');
        if (mode.Length == 0 || pbDigits.Length == 0 || !IsAllAsciiDigits(pbDigits))
        {
            return null;
        }

        if (!int.TryParse(pbStr, NumberStyles.Integer, CultureInfo.InvariantCulture, out var pb))
        {
            return null;
        }

        return (mode, pb);
    }

    /// <summary>
    /// True if a rigctld response line is an "RPRT &lt;code&gt;" error reply.
    /// On error, rigctld sends a single RPRT line instead of the normal
    /// response -- for 'm' that means only one line comes back, not two.
    /// </summary>
    public static bool IsRprtError(string line) => line.Trim().StartsWith("RPRT", StringComparison.Ordinal);

    /// <summary>
    /// Pure parser for a rigctld SET command's "RPRT &lt;code&gt;" reply. True
    /// only for "RPRT 0" (success); false for any other code, malformed
    /// reply, or null (I/O failure already logged by the caller).
    /// </summary>
    public static bool ParseSetResponse(string? line)
    {
        if (line is null) return false;
        var parts = line.Trim().Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        return parts.Length == 2 && parts[0] == "RPRT" && parts[1] == "0";
    }

    /// <summary>Pure parser for rigctld's 't' response: 0=RX, 1/2/3=TX (mic/data variants).</summary>
    public static bool? ParsePttResponse(string? resp)
    {
        if (resp is null) return null;
        var trimmed = resp.Trim();
        if (PttTxValues.Contains(trimmed)) return true;
        if (trimmed == "0") return false;
        return null;
    }

    private static bool IsAllAsciiDigits(string s)
    {
        foreach (var c in s)
        {
            if (!char.IsAsciiDigit(c)) return false;
        }

        return true;
    }
}
