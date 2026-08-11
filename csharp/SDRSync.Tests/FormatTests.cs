using SDRSync.Core;
using Xunit;

namespace SDRSync.Tests;

/// <summary>Ported from tests/test_gui_format.py.</summary>
public class FormatTests
{
    [Fact]
    public void FmtHz_Basic() => Assert.Equal("14.074.500", Format.FmtHz(14_074_500));

    [Fact]
    public void FmtHz_Zero() => Assert.Equal("0.000.000", Format.FmtHz(0));

    [Fact]
    public void FmtHz_Negative() => Assert.Equal("-14.074.500", Format.FmtHz(-14_074_500));

    [Fact]
    public void FmtHz_SubKhz() => Assert.Equal("0.000.500", Format.FmtHz(500));

    [Fact]
    public void FmtDelta_Positive() => Assert.Equal("+60 Hz", Format.FmtDelta(14_074_560, 14_074_500));

    [Fact]
    public void FmtDelta_Negative() => Assert.Equal("−140 Hz", Format.FmtDelta(14_074_360, 14_074_500));

    [Fact]
    public void FmtDelta_Zero() => Assert.Equal("+0 Hz", Format.FmtDelta(14_074_500, 14_074_500));

    [Fact]
    public void FmtHzSplit_Basic() => Assert.Equal(("14.074", "500"), Format.FmtHzSplit(14_074_500));

    [Fact]
    public void FmtHzSplit_Zero() => Assert.Equal(("0.000", "000"), Format.FmtHzSplit(0));

    [Fact]
    public void FmtHzSplit_Negative() => Assert.Equal(("-14.074", "500"), Format.FmtHzSplit(-14_074_500));

    [Fact]
    public void FmtHzSplit_SubKhz() => Assert.Equal(("0.000", "500"), Format.FmtHzSplit(500));
}
