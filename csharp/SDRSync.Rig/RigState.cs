namespace SDRSync.Rig;

/// <summary>
/// Shared status type for rig-control backends (rigctld, flrig), ported
/// from sdrsync/rig/base.py. Every field null means "this particular read
/// failed/dropped" -- never "value is legitimately unknown/zero".
/// </summary>
public sealed record RigState(int? FreqHz, string? Mode, int? PassbandHz, bool? Ptt);
