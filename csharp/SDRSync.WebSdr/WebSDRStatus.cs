namespace SDRSync.WebSdr;

/// <summary>
/// Ported from sdrsync/websdr/base.py's WebSDRStatus. Current status for
/// display in the GUI, and the source data for reverse sync (WebSDR -> rig).
///
/// Properties are mutable ({ get; set; }, not { get; init; }) -- the
/// Python original is a plain (non-frozen) dataclass, and both the real
/// engine's own driver-stub tests and this port's ported tests rely on
/// mutating a single shared instance in place across ticks to simulate
/// the WebSDR page's status changing (e.g. `status.freq_hz = 14200000`
/// between two `_tick()` calls) rather than constructing a new instance
/// each time. Still supports `with` expressions (used by
/// SyncEngine.Publish) regardless of mutability.
/// </summary>
public sealed record WebSDRStatus(
    bool Connected,
    int? FreqHz = null,
    string? Mode = null,
    bool? AudioActive = null,
    string? LastError = null)
{
    public bool Connected { get; set; } = Connected;
    public int? FreqHz { get; set; } = FreqHz;
    public string? Mode { get; set; } = Mode;
    public bool? AudioActive { get; set; } = AudioActive;
    public string? LastError { get; set; } = LastError;
}

/// <summary>
/// Raised when the live page doesn't expose the control API a driver
/// expects. Ported from sdrsync/websdr/base.py's WebSDRIncompatibleError --
/// the real defense against "the site's JS changed underneath us": fail
/// loudly and specifically, rather than reimplementing the site's wire
/// protocol or silently no-op'ing.
/// </summary>
public sealed class WebSDRIncompatibleException : Exception
{
    public WebSDRIncompatibleException(string message) : base(message)
    {
    }
}
