using SDRSync.Rig;
using Xunit;

namespace SDRSync.Tests;

/// <summary>Ported from tests/test_rigctld_parsing.py.</summary>
public class RigctldParsingTests
{
    [Fact]
    public void ParseFreq_Valid() => Assert.Equal(14074000, RigctldClient.ParseFreqResponse("14074000\n"));

    [Fact]
    public void ParseFreq_NoneInput() => Assert.Null(RigctldClient.ParseFreqResponse(null));

    [Fact]
    public void ParseFreq_Garbage() => Assert.Null(RigctldClient.ParseFreqResponse("RPRT -1"));

    [Fact]
    public void ParseMode_Valid() => Assert.Equal(("USB", 2400), RigctldClient.ParseModeResponse("USB\n", "2400\n"));

    [Fact]
    public void ParseMode_EmptyMode() => Assert.Null(RigctldClient.ParseModeResponse("", "2400"));

    [Fact]
    public void ParseMode_NonNumericPassband() => Assert.Null(RigctldClient.ParseModeResponse("USB", "wide"));

    [Fact]
    public void ParsePtt_Rx() => Assert.False(RigctldClient.ParsePttResponse("0\n"));

    [Fact]
    public void ParsePtt_TxVariants()
    {
        Assert.True(RigctldClient.ParsePttResponse("1"));
        Assert.True(RigctldClient.ParsePttResponse("2"));
        Assert.True(RigctldClient.ParsePttResponse("3"));
    }

    [Fact]
    public void ParsePtt_Unknown() => Assert.Null(RigctldClient.ParsePttResponse("9"));

    [Fact]
    public void ParsePtt_NoneInput() => Assert.Null(RigctldClient.ParsePttResponse(null));

    [Fact]
    public void IsRprtError_True() => Assert.True(RigctldClient.IsRprtError("RPRT -11\n"));

    [Fact]
    public void IsRprtError_FalseForNormalModeLine() => Assert.False(RigctldClient.IsRprtError("USB\n"));

    [Fact]
    public void ParseSetResponse_Success() => Assert.True(RigctldClient.ParseSetResponse("RPRT 0\n"));

    [Fact]
    public void ParseSetResponse_Rejection() => Assert.False(RigctldClient.ParseSetResponse("RPRT -11\n"));

    [Fact]
    public void ParseSetResponse_NoneInput() => Assert.False(RigctldClient.ParseSetResponse(null));

    [Fact]
    public void ParseSetResponse_Malformed()
    {
        Assert.False(RigctldClient.ParseSetResponse("garbage\n"));
        Assert.False(RigctldClient.ParseSetResponse("RPRT\n"));
    }
}
