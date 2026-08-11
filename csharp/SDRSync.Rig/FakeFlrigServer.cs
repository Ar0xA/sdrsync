using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Text;
using Microsoft.Extensions.Logging;
using SDRSync.Core;

namespace SDRSync.Rig;

/// <summary>
/// Mutable state for <see cref="FakeFlrigServer"/>, ported from
/// sdrsync/rig/fake_flrig.py's FakeFlrigState.
///
/// The Python original relies on CPython's GIL to make individual
/// attribute get/set atomic across threads (its XML-RPC server runs
/// synchronously in a background thread while the engine writes mock
/// values from its own event-loop thread). .NET has no equivalent
/// guarantee, so this uses an explicit lock around every read and write
/// instead -- a deliberate, required divergence from the Python source,
/// not an oversight.
/// </summary>
public sealed class FakeFlrigState : IMockRigState
{
    private readonly object _lock = new();

    private int _freqHz = 14_074_000;
    private string _mode = "USB";
    private int _passbandHz = 2400;
    private string _ptt = "0";
    private bool _setRejected;
    private int _transientDelayPolls;
    private int? _pendingFreqHz;
    private int _pendingFreqCountdown;
    private string? _pendingMode;
    private int _pendingModeCountdown;

    public int FreqHz
    {
        get { lock (_lock) return _freqHz; }
        set { lock (_lock) _freqHz = value; }
    }

    public string Mode
    {
        get { lock (_lock) return _mode; }
        set { lock (_lock) _mode = value; }
    }

    public int PassbandHz
    {
        get { lock (_lock) return _passbandHz; }
        set { lock (_lock) _passbandHz = value; }
    }

    public string Ptt
    {
        get { lock (_lock) return _ptt; }
        set { lock (_lock) _ptt = value; }
    }

    /// <summary>
    /// When true, set_vfoA()/set_mode() are accepted but never change
    /// state at all -- exercises FlrigClient's poll-until-match
    /// exhaustion/give-up path.
    /// </summary>
    public bool SetRejected
    {
        get { lock (_lock) return _setRejected; }
        set { lock (_lock) _setRejected = value; }
    }

    /// <summary>
    /// When &gt; 0, a set_vfoA()/set_mode() call doesn't apply immediately --
    /// it's staged as pending and only takes effect after this many
    /// subsequent get_vfo()/get_mode() polls, simulating real CAT-bus
    /// turnaround lag.
    /// </summary>
    public int TransientDelayPolls
    {
        get { lock (_lock) return _transientDelayPolls; }
        set { lock (_lock) _transientDelayPolls = value; }
    }

    public string GetVfo()
    {
        lock (_lock)
        {
            if (_pendingFreqHz is not null)
            {
                _pendingFreqCountdown--;
                if (_pendingFreqCountdown <= 0)
                {
                    _freqHz = _pendingFreqHz.Value;
                    _pendingFreqHz = null;
                }
            }

            return _freqHz.ToString(CultureInfo.InvariantCulture);
        }
    }

    public string GetMode()
    {
        lock (_lock)
        {
            if (_pendingMode is not null)
            {
                _pendingModeCountdown--;
                if (_pendingModeCountdown <= 0)
                {
                    _mode = _pendingMode;
                    _pendingMode = null;
                }
            }

            return _mode;
        }
    }

    public object?[] GetBw()
    {
        lock (_lock) return new object?[] { _passbandHz.ToString(CultureInfo.InvariantCulture), "" };
    }

    public int GetPtt()
    {
        lock (_lock) return int.Parse(_ptt, CultureInfo.InvariantCulture);
    }

    /// <summary>Real flrig's set_vfoA success path never sets a meaningful result either -- the RPC call always "succeeds" from the caller's perspective, only the resulting state (or lack of it) differs.</summary>
    public int SetVfoA(int freqHz)
    {
        lock (_lock)
        {
            if (!_setRejected)
            {
                if (_transientDelayPolls > 0)
                {
                    _pendingFreqHz = freqHz;
                    _pendingFreqCountdown = _transientDelayPolls;
                }
                else
                {
                    _freqHz = freqHz;
                }
            }
        }

        return 0;
    }

    public int SetMode(string modeName)
    {
        lock (_lock)
        {
            if (!_setRejected)
            {
                if (_transientDelayPolls > 0)
                {
                    _pendingMode = modeName;
                    _pendingModeCountdown = _transientDelayPolls;
                }
                else
                {
                    _mode = modeName;
                }
            }
        }

        return 0;
    }
}

/// <summary>
/// Minimal fake flrig XML-RPC server, for smoke-testing without real flrig
/// software, ported from sdrsync/rig/fake_flrig.py. Registers the RPC
/// methods FlrigClient actually calls: main.get_version, rig.get_vfo,
/// rig.get_mode, rig.get_bw, rig.get_ptt, rig.set_vfoA, rig.set_mode.
///
/// Implements HTTP/1.1 directly over a TcpListener rather than
/// System.Net.HttpListener -- avoids HttpListener's platform-specific
/// setup quirks (e.g. URL ACL reservations on Windows) for what only ever
/// needs to be a tiny single-endpoint loopback test server, mirroring how
/// RigctldClient's own fake server is a raw TCP listener rather than
/// reaching for a heavier framework.
/// </summary>
public sealed class FakeFlrigServer : IMockRigServer, IAsyncDisposable
{
    private readonly TcpListener _listener;
    private readonly FakeFlrigState _state;
    private readonly CancellationTokenSource _cts = new();
    private readonly Task _acceptLoop;

    private FakeFlrigServer(TcpListener listener, FakeFlrigState state)
    {
        _listener = listener;
        _state = state;
        _acceptLoop = Task.Run(() => AcceptLoopAsync(_cts.Token));
    }

    public FakeFlrigState State => _state;

    public int Port => ((IPEndPoint)_listener.LocalEndpoint).Port;

    public static Task<FakeFlrigServer> StartAsync(string host = "127.0.0.1", int port = 12345)
    {
        var listener = new TcpListener(IPAddress.Parse(host), port);
        listener.Start();
        var state = new FakeFlrigState();
        CoreLog.Logger.LogInformation("Fake flrig listening on {Host}:{Port}", host, ((IPEndPoint)listener.LocalEndpoint).Port);
        return Task.FromResult(new FakeFlrigServer(listener, state));
    }

    private async Task AcceptLoopAsync(CancellationToken ct)
    {
        try
        {
            while (!ct.IsCancellationRequested)
            {
                TcpClient client;
                try
                {
                    client = await _listener.AcceptTcpClientAsync(ct);
                }
                catch (OperationCanceledException)
                {
                    return;
                }
                catch (ObjectDisposedException)
                {
                    return;
                }

                _ = Task.Run(() => HandleClientAsync(client, ct), ct);
            }
        }
        catch (OperationCanceledException)
        {
            // shutting down
        }
    }

    private async Task HandleClientAsync(TcpClient client, CancellationToken ct)
    {
        using (client)
        await using (var stream = client.GetStream())
        {
            try
            {
                // HttpClient issues multiple XML-RPC requests over one
                // keep-alive connection (main.get_version at connect, then
                // many get_vfo/get_mode/... polls) -- loop until the peer closes.
                while (!ct.IsCancellationRequested)
                {
                    var requestBody = await ReadHttpRequestBodyAsync(stream, ct);
                    if (requestBody is null) return; // peer closed

                    string responseBody;
                    try
                    {
                        var decoded = XmlRpcCodec.DecodeRequest(requestBody);
                        var result = Dispatch(decoded.MethodName, decoded.Parameters);
                        responseBody = XmlRpcCodec.EncodeResponse(result);
                    }
                    catch (Exception e)
                    {
                        responseBody = XmlRpcCodec.EncodeFault(1, e.Message);
                    }

                    await WriteHttpResponseAsync(stream, responseBody, ct);
                }
            }
            catch (Exception e) when (e is IOException or OperationCanceledException or ObjectDisposedException)
            {
                // client disconnected / server shutting down
            }
        }
    }

    private object? Dispatch(string methodName, IReadOnlyList<object?> parameters) => methodName switch
    {
        "main.get_version" => "fake-flrig-1.0",
        "rig.get_vfo" => _state.GetVfo(),
        "rig.get_mode" => _state.GetMode(),
        "rig.get_bw" => _state.GetBw(),
        "rig.get_ptt" => _state.GetPtt(),
        "rig.set_vfoA" => _state.SetVfoA(Convert.ToInt32(parameters[0], CultureInfo.InvariantCulture)),
        "rig.set_mode" => _state.SetMode((string)parameters[0]!),
        _ => throw new InvalidOperationException($"Unknown method {methodName}"),
    };

    /// <summary>
    /// Reads one HTTP/1.1 request off the stream and returns its body
    /// text. Reads the header block byte-by-byte until the terminating
    /// blank line, then reads exactly Content-Length body bytes -- simple
    /// rather than efficient, which is fine for a loopback test server
    /// handling tiny XML-RPC payloads.
    /// </summary>
    private static async Task<string?> ReadHttpRequestBodyAsync(NetworkStream stream, CancellationToken ct)
    {
        var headerBytes = new List<byte>();
        var single = new byte[1];
        while (true)
        {
            int n;
            try
            {
                n = await stream.ReadAsync(single, ct);
            }
            catch (IOException)
            {
                return null;
            }

            if (n == 0)
            {
                return headerBytes.Count == 0 ? null : throw new IOException("Connection closed mid-request headers");
            }

            headerBytes.Add(single[0]);
            var c = headerBytes.Count;
            if (c >= 4 && headerBytes[c - 4] == (byte)'\r' && headerBytes[c - 3] == (byte)'\n'
                && headerBytes[c - 2] == (byte)'\r' && headerBytes[c - 1] == (byte)'\n')
            {
                break;
            }
        }

        var headerText = Encoding.ASCII.GetString(headerBytes.ToArray());
        var contentLength = 0;
        foreach (var line in headerText.Split("\r\n", StringSplitOptions.RemoveEmptyEntries))
        {
            var idx = line.IndexOf(':');
            if (idx < 0) continue;
            var name = line[..idx].Trim();
            if (string.Equals(name, "Content-Length", StringComparison.OrdinalIgnoreCase))
            {
                contentLength = int.Parse(line[(idx + 1)..].Trim(), CultureInfo.InvariantCulture);
            }
        }

        var body = new byte[contentLength];
        var read = 0;
        while (read < contentLength)
        {
            var n = await stream.ReadAsync(body.AsMemory(read, contentLength - read), ct);
            if (n == 0) throw new IOException("Connection closed mid-request body");
            read += n;
        }

        return Encoding.UTF8.GetString(body);
    }

    private static async Task WriteHttpResponseAsync(NetworkStream stream, string body, CancellationToken ct)
    {
        var bodyBytes = Encoding.UTF8.GetBytes(body);
        var header =
            $"HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\nContent-Length: {bodyBytes.Length}\r\nConnection: keep-alive\r\n\r\n";
        await stream.WriteAsync(Encoding.ASCII.GetBytes(header), ct);
        await stream.WriteAsync(bodyBytes, ct);
    }

    public void Close()
    {
        _cts.Cancel();
        _listener.Stop();
    }

    public async Task WaitClosedAsync()
    {
        try
        {
            await _acceptLoop;
        }
        catch (OperationCanceledException)
        {
            // expected on shutdown
        }
    }

    public async ValueTask DisposeAsync()
    {
        Close();
        await WaitClosedAsync();
    }
}
