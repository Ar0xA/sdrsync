using SDRSync.Rig;
using Xunit;

namespace SDRSync.Tests;

/// <summary>Ported from tests/test_flrig_parsing.py.</summary>
public class FlrigParsingTests
{
    [Fact]
    public void ParseFreq_Valid() => Assert.Equal(14070000, FlrigClient.ParseFreqResponse("14070000"));

    [Fact]
    public void ParseFreq_NoneInput() => Assert.Null(FlrigClient.ParseFreqResponse(null));

    [Fact]
    public void ParseFreq_NonStringInput() => Assert.Null(FlrigClient.ParseFreqResponse(14070000)); // real flrig always sends a string

    [Fact]
    public void ParseFreq_Garbage() => Assert.Null(FlrigClient.ParseFreqResponse("not a number"));

    [Fact]
    public void ParseMode_Valid() => Assert.Equal("USB", FlrigClient.ParseModeResponse("USB"));

    [Fact]
    public void ParseMode_StripsWhitespace() => Assert.Equal("USB", FlrigClient.ParseModeResponse(" USB \n"));

    [Fact]
    public void ParseMode_EmptyString() => Assert.Null(FlrigClient.ParseModeResponse(""));

    [Fact]
    public void ParseMode_NonStringInput() => Assert.Null(FlrigClient.ParseModeResponse(null));

    [Fact]
    public void ParseBandwidth_SingleTableShape() =>
        Assert.Equal(2400, FlrigClient.ParseBandwidthResponse(new object?[] { "2400", "" }));

    [Fact]
    public void ParseBandwidth_DualDspShapeUsesOnlyElementZero() =>
        Assert.Equal(1800, FlrigClient.ParseBandwidthResponse(new object?[] { "1800", "2800" }));

    [Fact]
    public void ParseBandwidth_NonNumericElementFallsBackToNone() =>
        Assert.Null(FlrigClient.ParseBandwidthResponse(new object?[] { "wide", "" }));

    [Fact]
    public void ParseBandwidth_EmptyList() => Assert.Null(FlrigClient.ParseBandwidthResponse(Array.Empty<object?>()));

    [Fact]
    public void ParseBandwidth_NonListInput()
    {
        Assert.Null(FlrigClient.ParseBandwidthResponse("2400"));
        Assert.Null(FlrigClient.ParseBandwidthResponse(null));
    }

    [Fact]
    public void ParsePtt_Rx() => Assert.False(FlrigClient.ParsePttResponse(0));

    [Fact]
    public void ParsePtt_Tx() => Assert.True(FlrigClient.ParsePttResponse(1));

    [Fact]
    public void ParsePtt_OtherNonzeroIntIsTx() => Assert.True(FlrigClient.ParsePttResponse(2));

    [Fact]
    public void ParsePtt_NonIntInput()
    {
        Assert.Null(FlrigClient.ParsePttResponse("1"));
        Assert.Null(FlrigClient.ParsePttResponse(null));
    }

    [Fact]
    public void ParsePtt_BoolInputIsRejectedNotSilentlyCoerced()
    {
        // An XML-RPC <boolean> decodes to a C# bool -- a structurally
        // distinct type from int, so `resp is int` alone already rejects
        // it (see FlrigClient.ParsePttResponse's doc comment).
        Assert.Null(FlrigClient.ParsePttResponse(true));
        Assert.Null(FlrigClient.ParsePttResponse(false));
    }
}
