namespace SDRSync.Sync;

/// <summary>
/// Tracks one in-flight (or backing-off-between-attempts) reverse-sync
/// retry ladder for a single axis (mode OR frequency) across ticks, ported
/// from sync/engine.py's _ReversePush. Target is the value being pushed
/// (a hamlib mode name string, or a frequency in Hz boxed as object to
/// mirror the Python original's untyped field -- callers always know
/// which axis they're reading).
/// </summary>
internal sealed class ReversePush(object target)
{
    public object Target { get; } = target;
    public int Attempts { get; set; }
    public double NextAttemptAt { get; set; }
}
