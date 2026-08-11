using System.Text.Json;
using SDRSync.WebSdr;
using Xunit;

namespace SDRSync.Tests;

/// <summary>Ported from tests/test_kiwisdr_mode_mapping.py, test_kiwisdr_status_mode.py, test_kiwisdr_reverse_mode_mapping.py.</summary>
public class KiwiSDRDriverTests
{
    // ------------------------------------------------------------------ mode mapping

    [Fact]
    public void UsbWidePassband_StaysWide() => Assert.Equal("usb", KiwiSDRDriver.MapHamlibModeKiwi("USB", 2700));

    [Fact]
    public void UsbNarrowPassband_GetsNarrowVariant() => Assert.Equal("usn", KiwiSDRDriver.MapHamlibModeKiwi("USB", 1800));

    [Fact]
    public void Lsb_CaseInsensitive() => Assert.Equal("lsb", KiwiSDRDriver.MapHamlibModeKiwi("lsb", 2700));

    [Fact]
    public void LsbNarrowPassband_GetsNarrowVariant() => Assert.Equal("lsn", KiwiSDRDriver.MapHamlibModeKiwi("LSB", 1800));

    [Fact]
    public void AmNarrowAndWideThresholds()
    {
        Assert.Equal("amn", KiwiSDRDriver.MapHamlibModeKiwi("AM", 1800));
        Assert.Equal("am", KiwiSDRDriver.MapHamlibModeKiwi("AM", 5000));
        Assert.Equal("amw", KiwiSDRDriver.MapHamlibModeKiwi("AM", 9000));
    }

    [Fact]
    public void CwAndCwr_BothMapToCwWithNarrowVariant()
    {
        Assert.Equal("cwn", KiwiSDRDriver.MapHamlibModeKiwi("CW", 500));
        Assert.Equal("cwn", KiwiSDRDriver.MapHamlibModeKiwi("CWR", 500));
        Assert.Equal("cw", KiwiSDRDriver.MapHamlibModeKiwi("CW", 2700));
    }

    [Fact]
    public void PacketModes_MapToTheirSideband()
    {
        Assert.Equal("usb", KiwiSDRDriver.MapHamlibModeKiwi("PKTUSB", 2700));
        Assert.Equal("lsb", KiwiSDRDriver.MapHamlibModeKiwi("PKTLSB", 2700));
    }

    [Fact]
    public void FmModes_MapToNbfmNarrowOrWide()
    {
        Assert.Equal("nnfm", KiwiSDRDriver.MapHamlibModeKiwi("FM", 1800));
        Assert.Equal("nbfm", KiwiSDRDriver.MapHamlibModeKiwi("WFM", 12000));
    }

    [Fact]
    public void Sam_HasNoNarrowVariant()
    {
        Assert.Equal("sam", KiwiSDRDriver.MapHamlibModeKiwi("SAM", 1800));
        Assert.Equal("sam", KiwiSDRDriver.MapHamlibModeKiwi("SAM", null));
    }

    [Fact]
    public void NoPassband_DefaultsToWide() => Assert.Equal("usb", KiwiSDRDriver.MapHamlibModeKiwi("USB", null));

    [Fact]
    public void UnknownMode_ReturnsNull() => Assert.Null(KiwiSDRDriver.MapHamlibModeKiwi("DSTAR", 2700));

    [Fact]
    public void DataModes_MapToTheirSideband()
    {
        Assert.Equal("usb", KiwiSDRDriver.MapHamlibModeKiwi("DATA-U", 2700));
        Assert.Equal("usn", KiwiSDRDriver.MapHamlibModeKiwi("DATA-U", 1800));
        Assert.Equal("lsb", KiwiSDRDriver.MapHamlibModeKiwi("DATA-L", 2700));
        Assert.Equal("lsn", KiwiSDRDriver.MapHamlibModeKiwi("DATA-L", 1800));
    }

    [Fact]
    public void CwUAndCwL_MapToPlainCw()
    {
        Assert.Equal("cwn", KiwiSDRDriver.MapHamlibModeKiwi("CW-U", 500));
        Assert.Equal("cwn", KiwiSDRDriver.MapHamlibModeKiwi("CW-L", 500));
        Assert.Equal("cw", KiwiSDRDriver.MapHamlibModeKiwi("CW-U", 2700));
        Assert.Equal("cw", KiwiSDRDriver.MapHamlibModeKiwi("CW-L", 2700));
    }

    [Fact]
    public void BaseModeOf_StripsNarrowVariants()
    {
        // This is what get_status() relies on to display "USB"/"LSB"
        // instead of the raw KiwiSDR-internal "usn"/"lsn" narrow-filter
        // mode strings ext_get_mode() returns verbatim.
        Assert.Equal("usb", KiwiSDRDriver.BaseModeOf("usn"));
        Assert.Equal("lsb", KiwiSDRDriver.BaseModeOf("lsn"));
        Assert.Equal("cw", KiwiSDRDriver.BaseModeOf("cwn"));
        Assert.Equal("am", KiwiSDRDriver.BaseModeOf("amn"));
        Assert.Equal("nbfm", KiwiSDRDriver.BaseModeOf("nnfm"));
    }

    [Fact]
    public void BaseModeOf_LeavesBaseModesAndUnknownsUnchanged()
    {
        Assert.Equal("usb", KiwiSDRDriver.BaseModeOf("usb"));
        Assert.Equal("lsb", KiwiSDRDriver.BaseModeOf("lsb"));
        Assert.Equal("amw", KiwiSDRDriver.BaseModeOf("amw"));
        Assert.Equal("sam", KiwiSDRDriver.BaseModeOf("sam"));
        Assert.Equal("iq", KiwiSDRDriver.BaseModeOf("iq"));
    }

    // ------------------------------------------------------------------ status mode normalization
    // get_status()'s displayed mode must be normalized to its base (non-narrow)
    // form -- ext_get_mode() returns KiwiSDR's own internal string verbatim
    // (e.g. "usn" for a narrow-filter USB selection), which reads as a
    // different mode than the hamlib USB it corresponds to if shown raw.
    // Regression coverage for the v9.1 fix.

    private sealed class StubPage : StubPageBase
    {
        private readonly JsonElement _result;
        public StubPage(object result) => _result = JsonSerializer.SerializeToElement(result);
        public override Task<JsonElement?> EvaluateAsync(string js, params object?[] args) => Task.FromResult<JsonElement?>(_result);
    }

    private static async Task<WebSDRStatus> GetStatus(string mode)
    {
        var driver = new KiwiSDRDriver("http://example.invalid");
        driver._attached = true;
        driver._page = new StubPage(new { freq = "14074.00", mode, audio = true });
        return await driver.GetStatusAsync();
    }

    [Fact]
    public async Task NarrowUsb_DisplaysAsUsb() => Assert.Equal("USB", (await GetStatus("usn")).Mode);

    [Fact]
    public async Task NarrowLsb_DisplaysAsLsb() => Assert.Equal("LSB", (await GetStatus("lsn")).Mode);

    [Fact]
    public async Task WideUsb_StillDisplaysAsUsb() => Assert.Equal("USB", (await GetStatus("usb")).Mode);

    [Fact]
    public async Task NarrowCw_DisplaysAsCw() => Assert.Equal("CW", (await GetStatus("cwn")).Mode);

    // ------------------------------------------------------------------ reverse mode mapping

    [Fact]
    public void ReverseUsb_MapsToUsb() => Assert.Equal("USB", KiwiSDRDriver.MapKiwiModeToHamlib("USB"));

    [Fact]
    public void ReverseLsb_MapsToLsb() => Assert.Equal("LSB", KiwiSDRDriver.MapKiwiModeToHamlib("LSB"));

    [Fact]
    public void ReverseCw_MapsToCw() => Assert.Equal("CW", KiwiSDRDriver.MapKiwiModeToHamlib("CW"));

    [Fact]
    public void ReverseAm_MapsToAm() => Assert.Equal("AM", KiwiSDRDriver.MapKiwiModeToHamlib("AM"));

    [Fact]
    public void ReverseAmw_AlsoMapsToAm_NoSeparateHamlibWideMode() => Assert.Equal("AM", KiwiSDRDriver.MapKiwiModeToHamlib("AMW"));

    [Fact]
    public void ReverseSam_MapsToSam() => Assert.Equal("SAM", KiwiSDRDriver.MapKiwiModeToHamlib("SAM"));

    [Fact]
    public void ReverseNbfm_MapsToFm() => Assert.Equal("FM", KiwiSDRDriver.MapKiwiModeToHamlib("NBFM"));

    [Fact]
    public void ReverseMapping_CaseInsensitive() => Assert.Equal("USB", KiwiSDRDriver.MapKiwiModeToHamlib("usb"));

    [Fact]
    public void ReverseMapping_NullInputReturnsNull() => Assert.Null(KiwiSDRDriver.MapKiwiModeToHamlib(null));

    [Fact]
    public void ReverseMapping_UnknownModeReturnsNull() => Assert.Null(KiwiSDRDriver.MapKiwiModeToHamlib("IQ"));
}
