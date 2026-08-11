namespace SDRSync.Core;

/// <summary>
/// One mutable instance every chrome/view reads from, ported from
/// sdrsync/gui/state.py.
/// </summary>
public sealed class AppState
{
    public bool RigConnected { get; set; }
    public bool SdrConnected { get; set; }

    /// <summary>
    /// True from the moment a WebSDR connect/switch is requested, before the
    /// page confirms connected. The receiver view needs this (not
    /// SdrConnected) to decide when to show the live receiver, because the
    /// embedded WebView must already be part of the visible window tree
    /// (nonzero size, a real native handle) before the underlying browser
    /// engine can initialize correctly -- the Python original hit this as a
    /// confirmed live bug ("Invalid window handle" from wx's MSW backend
    /// when the WebView was created while still hidden). Mirrors the
    /// RigActive/RigConnected distinction used for the same reason on the
    /// rig side.
    /// </summary>
    public bool SdrActive { get; set; }

    /// <summary>
    /// True whenever the main window is hidden in favour of the compact
    /// undocked bar. Pure GUI-side chrome state -- there is no
    /// StatusSnapshot/engine equivalent, it's set directly by the
    /// undock/dock handlers.
    /// </summary>
    public bool Undocked { get; set; }

    /// <summary>"transceiver" | "sites" | "behaviour" | null</summary>
    public string? OpenPanel { get; set; }

    public long RxHz { get; set; }

    /// <summary>Mirrors RxHz -- no real split/second-VFO polling exists.</summary>
    public long TxHz { get; set; }

    public long SdrHz { get; set; }
    public string Mode { get; set; } = "USB";
    public bool Ptt { get; set; }

    /// <summary>Session-only, engine-mirrored; never persisted.</summary>
    public bool Paused { get; set; }

    public bool MuteOnTx { get; set; } = true;
    public bool SyncTxVfo { get; set; } = true;
    public bool MockRig { get; set; }
    public string Site { get; set; } = "";
}
