using SDRSync.Core;
using SDRSync.WebSdr;

namespace SDRSync.Sync;

/// <summary>
/// Ported from sync/engine.py's StatusSnapshot. "Active" means "a session
/// has been requested/started" (may still be connecting/attaching);
/// "Connected"/Websdr presence means "actually up right now". The GUI
/// needs both to tell "connecting..." apart from "not connected because
/// never started" apart from "lost connection".
/// </summary>
public sealed record StatusSnapshot : GuiMessage
{
    /// <summary>
    /// Strictly increases on every StartRigAsync() call (including a
    /// same-tick mock-bind failure), never on a give-up alone -- so every
    /// snapshot published from one connect attempt, through its eventual
    /// give-up, shares one generation. Lets a future GUI distinguish a
    /// backlog of stale snapshots from a FAILED attempt (still draining
    /// from the status channel) from the first snapshot of a fresh
    /// attempt the user just started by clicking Connect again: filter out
    /// any snapshot whose generation isn't strictly newer than the
    /// generation captured at the moment of the click.
    /// </summary>
    public int RigGeneration { get; init; }

    public bool RigActive { get; init; }
    public bool RigConnected { get; init; }
    public int? RigFreqHz { get; init; }
    public string? RigMode { get; init; }
    public bool? RigPtt { get; init; }
    public string? RigError { get; init; }

    /// <summary>
    /// Seconds left before the engine gives up on the current connect
    /// attempt -- null once connected or when no attempt is in flight.
    /// Lets the status bar show a live "trying to connect (Ns)" countdown.
    /// </summary>
    public double? RigConnectRemainingS { get; init; }

    public bool WebsdrActive { get; init; }
    public WebSDRStatus? Websdr { get; init; }
    public string? FatalError { get; init; }

    /// <summary>
    /// Final/actionable: the reverse-sync retry ladder gave up and
    /// reverted the WebSDR page to match the rig (rig status is master).
    /// Cleared on the next successful reverse push or fresh WebSDR attach.
    /// </summary>
    public string? ReverseSyncError { get; init; }

    /// <summary>
    /// Transient/informational: an attempt already failed once and a retry
    /// is queued. Deliberately blank on the very first attempt, since a
    /// single unconfirmed readback is routine CAT-bus latency on some
    /// rigs, not worth surfacing.
    /// </summary>
    public string? ReverseSyncPending { get; init; }

    /// <summary>True while the user has paused the WebSDR -> rig direction via the Hold toggle.</summary>
    public bool ReverseSyncHeld { get; init; }

    /// <summary>True while the user has paused the rig -> WebSDR direction via the Pause sync toggle.</summary>
    public bool ForwardSyncPaused { get; init; }

    /// <summary>True while the WebSDR was disconnected by the app for idleness and is armed to reconnect automatically.</summary>
    public bool WebsdrIdleStopped { get; init; }
}
