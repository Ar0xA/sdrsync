using SDRSync.WebSdr;
using Xunit;

namespace SDRSync.Tests;

/// <summary>Ported from tests/test_openwebrx_mode_mapping.py, test_openwebrx_reverse_mode_mapping.py.</summary>
public class OpenWebRXDriverTests
{
    [Fact]
    public void DirectModeMapping()
    {
        Assert.Equal("usb", OpenWebRXDriver.MapHamlibModeOpenwebrx("USB"));
        Assert.Equal("lsb", OpenWebRXDriver.MapHamlibModeOpenwebrx("LSB"));
        Assert.Equal("cw", OpenWebRXDriver.MapHamlibModeOpenwebrx("CW"));
        Assert.Equal("am", OpenWebRXDriver.MapHamlibModeOpenwebrx("AM"));
        Assert.Equal("nfm", OpenWebRXDriver.MapHamlibModeOpenwebrx("FM"));
        Assert.Equal("wfm", OpenWebRXDriver.MapHamlibModeOpenwebrx("WFM"));
    }

    [Fact]
    public void CaseInsensitive()
    {
        Assert.Equal("usb", OpenWebRXDriver.MapHamlibModeOpenwebrx("usb"));
        Assert.Equal("cw", OpenWebRXDriver.MapHamlibModeOpenwebrx("Cw"));
    }

    [Fact]
    public void Cwr_MapsToCw() => Assert.Equal("cw", OpenWebRXDriver.MapHamlibModeOpenwebrx("CWR"));

    [Fact]
    public void PacketModes_MapToTheirSideband()
    {
        Assert.Equal("usb", OpenWebRXDriver.MapHamlibModeOpenwebrx("PKTUSB"));
        Assert.Equal("lsb", OpenWebRXDriver.MapHamlibModeOpenwebrx("PKTLSB"));
    }

    [Fact]
    public void UnknownMode_ReturnsNull() => Assert.Null(OpenWebRXDriver.MapHamlibModeOpenwebrx("DSTAR"));

    [Fact]
    public void DataModes_MapToTheirSideband()
    {
        Assert.Equal("usb", OpenWebRXDriver.MapHamlibModeOpenwebrx("DATA-U"));
        Assert.Equal("lsb", OpenWebRXDriver.MapHamlibModeOpenwebrx("DATA-L"));
    }

    [Fact]
    public void CwUAndCwL_MapToPlainCw()
    {
        Assert.Equal("cw", OpenWebRXDriver.MapHamlibModeOpenwebrx("CW-U"));
        Assert.Equal("cw", OpenWebRXDriver.MapHamlibModeOpenwebrx("CW-L"));
    }

    // ------------------------------------------------------------------ reverse mode mapping

    [Fact]
    public void ReverseUsb_MapsToUsb() => Assert.Equal("USB", OpenWebRXDriver.MapOpenwebrxModeToHamlib("USB"));

    [Fact]
    public void ReverseLsb_MapsToLsb() => Assert.Equal("LSB", OpenWebRXDriver.MapOpenwebrxModeToHamlib("LSB"));

    [Fact]
    public void ReverseCw_MapsToCw() => Assert.Equal("CW", OpenWebRXDriver.MapOpenwebrxModeToHamlib("CW"));

    [Fact]
    public void ReverseAm_MapsToAm() => Assert.Equal("AM", OpenWebRXDriver.MapOpenwebrxModeToHamlib("AM"));

    [Fact]
    public void ReverseNfm_MapsToFm() => Assert.Equal("FM", OpenWebRXDriver.MapOpenwebrxModeToHamlib("NFM"));

    [Fact]
    public void ReverseWfm_MapsToWfm() => Assert.Equal("WFM", OpenWebRXDriver.MapOpenwebrxModeToHamlib("WFM"));

    [Fact]
    public void ReverseMapping_CaseInsensitive() => Assert.Equal("USB", OpenWebRXDriver.MapOpenwebrxModeToHamlib("usb"));

    [Fact]
    public void ReverseMapping_NullInputReturnsNull() => Assert.Null(OpenWebRXDriver.MapOpenwebrxModeToHamlib(null));

    [Fact]
    public void ReverseMapping_UnknownModeReturnsNull() => Assert.Null(OpenWebRXDriver.MapOpenwebrxModeToHamlib("DIGITAL"));
}
