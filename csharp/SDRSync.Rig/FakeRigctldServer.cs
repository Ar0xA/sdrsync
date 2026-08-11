using System.Net;
using System.Net.Sockets;
using System.Text;
using Microsoft.Extensions.Logging;
using SDRSync.Core;

namespace SDRSync.Rig;

/// <summary>
/// Mutable state for <see cref="FakeRigctldServer"/>, ported from
/// sdrsync/rig/fake_rigctld.py's FakeRigState.
/// </summary>
public sealed class FakeRigctldState : IMockRigState
{
    public int FreqHz { get; set; } = 14_074_000;
    public string Mode { get; set; } = "USB";
    public int PassbandHz { get; set; } = 2400;
    public string Ptt { get; set; } = "0";

    /// <summary>
    /// When true, 'm' replies with a single RPRT error line instead of the
    /// normal two-line mode/passband response -- lets tests exercise
    /// RigctldClient.GetModeAsync()'s handling of that case without a real rig.
    /// </summary>
    public bool ModeError;

    /// <summary>
    /// When true, 'F'/'M' (reverse-sync SET commands) reply with RPRT -11
    /// (rejected) instead of applying the change and replying RPRT 0.
    /// </summary>
    public bool SetRejected;

    /// <summary>
    /// Number of upcoming 'f'/'m' GET polls to keep reporting the OLD value
    /// after an accepted 'F'/'M' SET command, before the new value becomes
    /// visible -- simulates real CAT-bus turnaround lag. Set to a huge
    /// number to simulate a command that never actually lands (tests the
    /// give-up path) rather than one that's merely slow. Independent per
    /// field, decremented only by GET polls of that field.
    /// </summary>
    public int FreqApplyAfterPolls;

    public int ModeApplyAfterPolls;

    internal int? PendingFreqHz;
    internal string? PendingMode;
    internal int? PendingPassbandHz;
}

/// <summary>
/// Minimal fake rigctld TCP server, for smoke-testing without real
/// hardware, ported from sdrsync/rig/fake_rigctld.py. Speaks just enough
/// of hamlib's rigctld text protocol for this app: f, m, t, v, F, M.
/// </summary>
public sealed class FakeRigctldServer : IMockRigServer, IAsyncDisposable
{
    private readonly TcpListener _listener;
    private readonly FakeRigctldState _state;
    private readonly CancellationTokenSource _cts = new();
    private readonly Task _acceptLoop;

    private FakeRigctldServer(TcpListener listener, FakeRigctldState state)
    {
        _listener = listener;
        _state = state;
        _acceptLoop = Task.Run(() => AcceptLoopAsync(_cts.Token));
    }

    public FakeRigctldState State => _state;

    public int Port => ((IPEndPoint)_listener.LocalEndpoint).Port;

    /// <summary>
    /// Starts listening. Port 0 picks an ephemeral free port (read back via
    /// <see cref="Port"/>), mirroring the Python original's asyncio.start_server(port=0) usage in tests.
    /// </summary>
    public static Task<FakeRigctldServer> StartAsync(string host = "127.0.0.1", int port = 4532)
    {
        var listener = new TcpListener(IPAddress.Parse(host), port);
        listener.Start();
        var state = new FakeRigctldState();
        CoreLog.Logger.LogInformation("Fake rigctld listening on {Host}:{Port}", host, ((IPEndPoint)listener.LocalEndpoint).Port);
        return Task.FromResult(new FakeRigctldServer(listener, state));
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
            var reader = new StreamReader(stream, Encoding.ASCII);
            try
            {
                while (!ct.IsCancellationRequested)
                {
                    var line = await reader.ReadLineAsync(ct);
                    if (line is null) break; // peer closed
                    var cmd = line.Trim();
                    var response = HandleCommand(cmd);
                    var bytes = Encoding.ASCII.GetBytes(response);
                    await stream.WriteAsync(bytes, ct);
                }
            }
            catch (IOException)
            {
                // client disconnected mid-read/write
            }
            catch (OperationCanceledException)
            {
                // server shutting down
            }
        }
    }

    private string HandleCommand(string cmd)
    {
        if (cmd == "f")
        {
            if (_state.PendingFreqHz is not null)
            {
                if (_state.FreqApplyAfterPolls > 0)
                {
                    _state.FreqApplyAfterPolls--;
                }
                else
                {
                    _state.FreqHz = _state.PendingFreqHz.Value;
                    _state.PendingFreqHz = null;
                }
            }

            return $"{_state.FreqHz}\n";
        }

        if (cmd == "m")
        {
            if (_state.PendingMode is not null)
            {
                if (_state.ModeApplyAfterPolls > 0)
                {
                    _state.ModeApplyAfterPolls--;
                }
                else
                {
                    _state.Mode = _state.PendingMode;
                    _state.PassbandHz = _state.PendingPassbandHz ?? _state.PassbandHz;
                    _state.PendingMode = null;
                    _state.PendingPassbandHz = null;
                }
            }

            return _state.ModeError ? "RPRT -11\n" : $"{_state.Mode}\n{_state.PassbandHz}\n";
        }

        if (cmd == "t")
        {
            return $"{_state.Ptt}\n";
        }

        if (cmd == "v")
        {
            return "Dummy\n";
        }

        if (cmd.StartsWith("F ", StringComparison.Ordinal))
        {
            var parts = cmd.Split(' ', StringSplitOptions.RemoveEmptyEntries);
            if (_state.SetRejected || parts.Length != 2 || !IsSignedInteger(parts[1]))
            {
                return "RPRT -11\n";
            }

            _state.PendingFreqHz = int.Parse(parts[1]);
            return "RPRT 0\n";
        }

        if (cmd.StartsWith("M ", StringComparison.Ordinal))
        {
            var parts = cmd.Split(' ', StringSplitOptions.RemoveEmptyEntries);
            if (_state.SetRejected || parts.Length != 3 || !IsSignedInteger(parts[2]))
            {
                return "RPRT -11\n";
            }

            _state.PendingMode = parts[1];
            _state.PendingPassbandHz = int.Parse(parts[2]);
            return "RPRT 0\n";
        }

        return "RPRT -11\n";
    }

    private static bool IsSignedInteger(string s)
    {
        var digits = s.TrimStart('-');
        return digits.Length > 0 && digits.All(char.IsAsciiDigit);
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
