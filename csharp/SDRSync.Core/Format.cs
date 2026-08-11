namespace SDRSync.Core;

/// <summary>
/// Formatting helpers, ported 1:1 from sdrsync/gui/format.py. Pure
/// functions, no UI dependency.
/// </summary>
public static class Format
{
    /// <summary>14074500 -> "14.074.500"</summary>
    public static string FmtHz(long hz)
    {
        var (mhz, khz, hzr) = SplitParts(hz, out var sign);
        return $"{sign}{mhz}.{khz:D3}.{hzr:D3}";
    }

    /// <summary>-> "+60 Hz" / "−40 Hz" (real minus sign, U+2212, not ASCII hyphen)</summary>
    public static string FmtDelta(long a, long b)
    {
        var d = a - b;
        var signChar = d >= 0 ? "+" : "−";
        return $"{signChar}{Math.Abs(d)} Hz";
    }

    /// <summary>
    /// 14074500 -> ("14.074", "500") -- kHz group / Hz remainder, for the
    /// compact bar's two-size frequency display: the kHz group renders in
    /// the big frequency font, the Hz remainder smaller and muted alongside it.
    /// </summary>
    public static (string KhzGroup, string HzRemainder) FmtHzSplit(long hz)
    {
        var (mhz, khz, hzr) = SplitParts(hz, out var sign);
        return ($"{sign}{mhz}.{khz:D3}", $"{hzr:D3}");
    }

    private static (long Mhz, long Khz, long Hzr) SplitParts(long hz, out string sign)
    {
        sign = hz < 0 ? "-" : "";
        var abs = Math.Abs(hz);
        var mhz = abs / 1_000_000;
        var rest = abs % 1_000_000;
        var khz = rest / 1_000;
        var hzr = rest % 1_000;
        return (mhz, khz, hzr);
    }
}
