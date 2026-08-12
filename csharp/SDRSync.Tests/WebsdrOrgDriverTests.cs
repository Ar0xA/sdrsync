using System.Text.Json;
using Microsoft.Extensions.Logging;
using SDRSync.Core;
using SDRSync.WebSdr;
using Xunit;

namespace SDRSync.Tests;

/// <summary>Ported from tests/test_websdr_org_band_selection.py, test_websdr_org_mode_mapping.py, test_websdr_org_reverse_mode_mapping.py, test_websdr_org_status_mode.py, test_websdr_org_verify_flag.py.</summary>
public class WebsdrOrgDriverTests
{
    /// <summary>
    /// Records every log call made through CoreLog.Logger while installed
    /// -- the C# analog of pytest's caplog fixture, needed for the
    /// rate-limited-warning regression test below. CoreLog.Logger is a
    /// shared static, so tests that use this must restore the original
    /// logger afterward (see WithCapturedLogs) -- and since xUnit runs
    /// different test classes in parallel, some OTHER test elsewhere in
    /// the assembly can genuinely still be logging through this same
    /// swapped-in instance while ours enumerates it (confirmed live: the
    /// frequency-readback verify test below holds the swap open across a
    /// real ~0.6s delay, long enough to reliably collide). A
    /// ConcurrentQueue, not a plain List, is required here -- not a style
    /// choice.
    /// </summary>
    private sealed class ListLogger : ILogger
    {
        public System.Collections.Concurrent.ConcurrentQueue<(LogLevel Level, string Message)> Records { get; } = new();

        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;
        public bool IsEnabled(LogLevel logLevel) => true;

        public void Log<TState>(LogLevel logLevel, EventId eventId, TState state, Exception? exception, Func<TState, Exception?, string> formatter) =>
            Records.Enqueue((logLevel, formatter(state, exception)));
    }

    private static void WithCapturedLogs(Action<ListLogger> body)
    {
        var original = CoreLog.Logger;
        var logger = new ListLogger();
        CoreLog.Logger = logger;
        try
        {
            body(logger);
        }
        finally
        {
            CoreLog.Logger = original;
        }
    }

    // ------------------------------------------------------------------ band selection

    private static WebsdrOrgDriver MakeDriver(params (double CenterKhz, double SamplerateKhz)[] bands)
    {
        var d = new WebsdrOrgDriver("http://example.invalid/");
        d._bands = bands.Select(b => (b.CenterKhz * 1000 - b.SamplerateKhz * 1000 / 2, b.CenterKhz * 1000 + b.SamplerateKhz * 1000 / 2)).ToList();
        return d;
    }

    [Fact]
    public void SingleWidebandLikeTwente_CoversFullRange()
    {
        // Twente: centerfreq=14579.8 kHz, samplerate=29159.6 kHz -> ~0 to 29.16 MHz
        var d = MakeDriver((14579.8, 29159.6));
        Assert.Equal(0, d.BandForFreq(14_074_000));
        Assert.Equal(0, d.BandForFreq(100_000));
        Assert.Equal(0, d.BandForFreq(29_000_000));
    }

    [Fact]
    public void FrequencyOutsideAllBands_ReturnsNull()
    {
        var d = MakeDriver((14579.8, 29159.6));
        Assert.Null(d.BandForFreq(50_000_000));
    }

    [Fact]
    public void MultiBandSite_PicksCorrectBand()
    {
        // A 40m band centered at 7100 kHz (200 kHz wide) and a 20m band at 14200 (200 kHz wide).
        var d = MakeDriver((7100.0, 200.0), (14200.0, 200.0));
        Assert.Equal(0, d.BandForFreq(7_150_000));
        Assert.Equal(1, d.BandForFreq(14_250_000));
        Assert.Null(d.BandForFreq(10_000_000));
    }

    [Fact]
    public async Task OutOfRangeWarning_IsRateLimitedOnExactRepeats()
    {
        // v14: the engine keeps retrying a rejected forward push on its own
        // schedule, so a rig parked on a frequency this receiver simply
        // doesn't cover would otherwise emit an identical WARNING forever.
        // WARNING when the condition changes, DEBUG on exact repeats.
        var d = MakeDriver((7100.0, 200.0));
        d._attached = true;

        WithCapturedLogs(logger =>
        {
            Assert.False(SyncRun(d.TuneHzAsync(14_074_000)));
            Assert.Single(logger.Records.Where(r => r.Level == LogLevel.Warning));

            logger.Records.Clear();
            Assert.False(SyncRun(d.TuneHzAsync(14_074_000))); // same frequency, still rejected
            Assert.Empty(logger.Records.Where(r => r.Level == LogLevel.Warning));
            Assert.Contains(logger.Records, r => r.Level == LogLevel.Debug);

            logger.Records.Clear();
            Assert.False(SyncRun(d.TuneHzAsync(21_000_000))); // a DIFFERENT rejected frequency
            Assert.Single(logger.Records.Where(r => r.Level == LogLevel.Warning));
        });
    }

    [Fact]
    public async Task OutOfRangeMessage_IsStillReportedToTheGuiOnRepeats()
    {
        // Rate-limiting the LOG must not rate-limit the status the GUI
        // shows -- _lastTuneError is what surfaces the rejection on screen.
        var d = MakeDriver((7100.0, 200.0));
        d._attached = true;

        await d.TuneHzAsync(14_074_000);
        await d.TuneHzAsync(14_074_000);
        Assert.NotNull(d._lastTuneError);
        Assert.Contains("outside all bands", d._lastTuneError);
    }

    private static bool SyncRun(Task<bool> task) => task.GetAwaiter().GetResult();

    // ------------------------------------------------------------------ mode mapping

    [Fact]
    public void UsbWidePassband_StaysWide() => Assert.Equal("USB", WebsdrOrgDriver.MapHamlibMode("USB", 2700));

    [Fact]
    public void UsbNarrowPassband_GetsNarrowVariant() => Assert.Equal("USBN", WebsdrOrgDriver.MapHamlibMode("USB", 1800));

    [Fact]
    public void Lsb_CaseInsensitive() => Assert.Equal("LSB", WebsdrOrgDriver.MapHamlibMode("lsb", 2700));

    [Fact]
    public void AmNarrowThreshold_DiffersFromUsb()
    {
        // 5000 Hz is "wide" for USB/LSB (>2000) but "narrow" for AM (<6000).
        Assert.Equal("AMN", WebsdrOrgDriver.MapHamlibMode("AM", 5000));
        Assert.Equal("USB", WebsdrOrgDriver.MapHamlibMode("USB", 5000));
    }

    [Fact]
    public void CwAndCwr_BothMapToCwNoNarrowVariant()
    {
        Assert.Equal("CW", WebsdrOrgDriver.MapHamlibMode("CW", 500));
        Assert.Equal("CW", WebsdrOrgDriver.MapHamlibMode("CWR", 500));
    }

    [Fact]
    public void PacketModes_MapToTheirSideband()
    {
        Assert.Equal("USB", WebsdrOrgDriver.MapHamlibMode("PKTUSB", 2700));
        Assert.Equal("LSB", WebsdrOrgDriver.MapHamlibMode("PKTLSB", 2700));
    }

    [Fact]
    public void NoPassband_DefaultsToWide() => Assert.Equal("USB", WebsdrOrgDriver.MapHamlibMode("USB", null));

    [Fact]
    public void UnknownMode_ReturnsNull() => Assert.Null(WebsdrOrgDriver.MapHamlibMode("DSTAR", 2700));

    [Fact]
    public void DataModes_MapToTheirSideband()
    {
        Assert.Equal("USB", WebsdrOrgDriver.MapHamlibMode("DATA-U", 2700));
        Assert.Equal("USBN", WebsdrOrgDriver.MapHamlibMode("DATA-U", 1800));
        Assert.Equal("LSB", WebsdrOrgDriver.MapHamlibMode("DATA-L", 2700));
        Assert.Equal("LSBN", WebsdrOrgDriver.MapHamlibMode("DATA-L", 1800));
    }

    [Fact]
    public void CwUAndCwL_MapToPlainCwNoNarrowVariant()
    {
        Assert.Equal("CW", WebsdrOrgDriver.MapHamlibMode("CW-U", 500));
        Assert.Equal("CW", WebsdrOrgDriver.MapHamlibMode("CW-L", 500));
    }

    // ------------------------------------------------------------------ reverse mode mapping

    [Fact]
    public void ReverseUsb_MapsToUsb() => Assert.Equal("USB", WebsdrOrgDriver.MapWebsdrModeToHamlib("USB"));

    [Fact]
    public void ReverseLsb_MapsToLsb() => Assert.Equal("LSB", WebsdrOrgDriver.MapWebsdrModeToHamlib("LSB"));

    [Fact]
    public void ReverseCw_MapsToCw() => Assert.Equal("CW", WebsdrOrgDriver.MapWebsdrModeToHamlib("CW"));

    [Fact]
    public void ReverseAm_MapsToAm() => Assert.Equal("AM", WebsdrOrgDriver.MapWebsdrModeToHamlib("AM"));

    [Fact]
    public void ReverseAmsync_MapsToSam() => Assert.Equal("SAM", WebsdrOrgDriver.MapWebsdrModeToHamlib("AMSYNC"));

    [Fact]
    public void ReverseFm_MapsToFm() => Assert.Equal("FM", WebsdrOrgDriver.MapWebsdrModeToHamlib("FM"));

    [Fact]
    public void ReverseMapping_CaseInsensitive() => Assert.Equal("USB", WebsdrOrgDriver.MapWebsdrModeToHamlib("usb"));

    [Fact]
    public void ReverseMapping_NullInputReturnsNull() => Assert.Null(WebsdrOrgDriver.MapWebsdrModeToHamlib(null));

    [Fact]
    public void ReverseMapping_UnknownModeReturnsNull() => Assert.Null(WebsdrOrgDriver.MapWebsdrModeToHamlib("DSTAR"));

    // ------------------------------------------------------------------ status mode normalization
    // get_status()'s displayed mode must be normalized to its base (non-narrow)
    // form -- window.mode can legitimately be e.g. "USBN" for a narrow-filter
    // USB selection, which reads as a different mode than the hamlib USB it
    // actually corresponds to if shown raw. Regression coverage for the v9.1 fix.

    private sealed class StubPage : StubPageBase
    {
        private readonly JsonElement _result;
        public StubPage(object result) => _result = JsonSerializer.SerializeToElement(result);
        public override Task<JsonElement?> EvaluateAsync(string js, params object?[] args) => Task.FromResult<JsonElement?>(_result);
    }

    private static async Task<WebSDRStatus> GetStatus(string mode)
    {
        var driver = new WebsdrOrgDriver("http://example.invalid");
        driver._attached = true;
        driver._page = new StubPage(new { freq = 14074.0, mode, audio = true });
        return await driver.GetStatusAsync();
    }

    [Fact]
    public async Task NarrowUsb_DisplaysAsUsb() => Assert.Equal("USB", (await GetStatus("USBN")).Mode);

    [Fact]
    public async Task NarrowLsb_DisplaysAsLsb() => Assert.Equal("LSB", (await GetStatus("LSBN")).Mode);

    [Fact]
    public async Task WideUsb_StillDisplaysAsUsb() => Assert.Equal("USB", (await GetStatus("USB")).Mode);

    [Fact]
    public async Task NarrowAm_DisplaysAsAm() => Assert.Equal("AM", (await GetStatus("AMN")).Mode);

    [Fact]
    public async Task Amsync_IsNotMistakenForANarrowMode() => Assert.Equal("AMSYNC", (await GetStatus("AMSYNC")).Mode);

    // ------------------------------------------------------------------ verify flag
    // tune_hz(verify=False) must not arm the delayed corrective re-tune task
    // (ScheduleFreqVerification/VerifyFreqAppliedAsync) -- this is what stops
    // the engine's periodic full-resync of an ALREADY-unchanged frequency from
    // fighting a genuine, concurrent reverse-sync push landing in the same
    // ~0.6s verification window (v12). verify=True (the default, used for
    // every real frequency change) must keep scheduling it exactly as before.

    private sealed class RecordingPage : StubPageBase
    {
        public List<(string Js, object?[] Args)> Calls { get; } = new();
        public override Task<JsonElement?> EvaluateAsync(string js, params object?[] args)
        {
            Calls.Add((js, args));
            return Task.FromResult<JsonElement?>(null);
        }
    }

    private static WebsdrOrgDriver MakeVerifyDriver()
    {
        var driver = new WebsdrOrgDriver("http://example.invalid/");
        driver._attached = true;
        driver._bands = new List<(double, double)> { (0, 30_000_000) };
        driver._currentBand = 0;
        driver._page = new RecordingPage();
        return driver;
    }

    /// <summary>Runs TuneHzAsync and reports whether a verify task was scheduled, cleanly cancelling it afterward (it sleeps FREQ_VERIFY_DELAY_S before doing anything else, so it never actually runs its body here).</summary>
    private static async Task<(bool Ok, bool Scheduled)> TuneAndCapture(WebsdrOrgDriver driver, int freqHz, bool? verify)
    {
        var ok = verify is null ? await driver.TuneHzAsync(freqHz) : await driver.TuneHzAsync(freqHz, verify.Value);
        var task = driver._verifyTask;
        var scheduled = task is not null;
        if (task is not null)
        {
            try { await task; } catch { /* cancelled or errored -- irrelevant to this test */ }
        }

        return (ok, scheduled);
    }

    [Fact]
    public async Task VerifyTrue_SchedulesCorrectiveVerification()
    {
        var driver = MakeVerifyDriver();
        var (ok, scheduled) = await TuneAndCapture(driver, 14_074_000, true);
        Assert.True(ok);
        Assert.True(scheduled);
    }

    [Fact]
    public async Task VerifyFalse_DoesNotScheduleCorrectiveVerification()
    {
        var driver = MakeVerifyDriver();
        var (ok, scheduled) = await TuneAndCapture(driver, 14_074_000, false);
        Assert.True(ok);
        Assert.False(scheduled);
    }

    [Fact]
    public async Task VerifyDefaultsToTrue()
    {
        var driver = MakeVerifyDriver();
        var (ok, scheduled) = await TuneAndCapture(driver, 14_074_000, true);

        var driver2 = MakeVerifyDriver();
        var (ok2, scheduled2) = await TuneAndCapture(driver2, 14_074_000, null); // no verify arg at all

        Assert.True(ok && scheduled);
        Assert.True(ok2 && scheduled2);
    }

    // ------------------------------------------------------------------ frequency readback (v2.2.2)
    // Regression guard for a bug found by driving the REAL WebsdrOrgDriver
    // against the REAL Twente site: the driver read back the tuned
    // frequency from a `#freqinput` element that DOES NOT EXIST on any
    // websdr.org build -- verified live against five instances (Twente,
    // Hack Green, IS0GRB, NA5B, Maasbree). The visible box is
    // `document.freqform.frequency` (a `name`, no `id`). So
    // ReadPageFreqHzAsync() always returned null, VerifyFreqAppliedAsync()
    // was permanently dead, and every single tune logged "Could not read
    // back ... to verify setfreq()" (hundreds of such lines in a real
    // user's log). The fix reads the page's own global `window.freq`
    // (kHz) instead, ported to sdrsync/websdr/websdr_org.py's
    // _read_page_freq_hz() this same session.
    //
    // ReadbackStubPage below models the real page faithfully: no
    // #freqinput, a `freq` global stored by setfreq(), and a display
    // string that is rounded AND CW-shifted exactly like the real one.

    /// <summary>Faithful stand-in for a real websdr.org page's JS surface, ported from test_websdr_org_freq_readback.py's StubWebsdrPage.</summary>
    private sealed class ReadbackStubPage : StubPageBase
    {
        // websdr-base.js set_mode("CW") -> setmf("cw", -0.95, -0.55); the
        // display shows nominalfreq() = freq + (hi+lo)/2 = freq - 0.75 kHz.
        private const double CwLoKhz = -0.95, CwHiKhz = -0.55;
        private const double SsbLoKhz = 0.3, SsbHiKhz = 2.7;

        public double? FreqKhz;
        private double _lo = SsbLoKhz, _hi = SsbHiKhz;
        public List<double> SetfreqCalls { get; } = new();
        public List<int> SetbandCalls { get; } = new();

        /// <summary>What the real page would show in document.freqform.frequency.</summary>
        public string Display()
        {
            if (FreqKhz is null) return "";
            var nominal = FreqKhz.Value;
            if (_lo < 0 && _hi < 0) nominal = FreqKhz.Value + (_hi + _lo) / 2; // iscw()
            return nominal.ToString("F2", System.Globalization.CultureInfo.InvariantCulture); // <- the real 10 Hz rounding
        }

        public override Task<JsonElement?> EvaluateAsync(string js, params object?[] args)
        {
            if (js.Contains("window.setfreq"))
            {
                FreqKhz = Convert.ToDouble(args[0], System.Globalization.CultureInfo.InvariantCulture);
                SetfreqCalls.Add(FreqKhz.Value);
                return Task.FromResult<JsonElement?>(null);
            }

            if (js.Contains("window.setband"))
            {
                SetbandCalls.Add(Convert.ToInt32(args[0], System.Globalization.CultureInfo.InvariantCulture));
                return Task.FromResult<JsonElement?>(null);
            }

            if (js.Contains("window.set_mode"))
            {
                var mode = args[0]?.ToString()?.ToUpperInvariant() ?? "";
                (_lo, _hi) = mode.StartsWith("CW", StringComparison.Ordinal) ? (CwLoKhz, CwHiKhz) : (SsbLoKhz, SsbHiKhz);
                return Task.FromResult<JsonElement?>(null);
            }

            if (js.Contains("window.freq"))
            {
                return Task.FromResult(FreqKhz is null ? (JsonElement?)null : JsonSerializer.SerializeToElement(FreqKhz.Value));
            }

            // The real page has no #freqinput element -- this is the whole point.
            return Task.FromResult<JsonElement?>(null);
        }
    }

    private static WebsdrOrgDriver MakeReadbackDriver(int cwOffsetHz = 0)
    {
        var driver = new WebsdrOrgDriver("http://example.invalid/", cwOffsetHz);
        driver._attached = true;
        driver._bands = new List<(double, double)> { (0, 30_000_000) };
        driver._currentBand = 0;
        driver._page = new ReadbackStubPage();
        return driver;
    }

    private static async Task<int?> TuneThenRead(WebsdrOrgDriver driver, int freqHz)
    {
        await driver.TuneHzAsync(freqHz, verify: false);
        return await driver.ReadPageFreqHzAsync();
    }

    [Theory]
    [InlineData(14_074_500)]
    [InlineData(14_074_499)]
    [InlineData(14_074_498)]
    [InlineData(14_075_000)]
    public async Task Readback_IsExactToOneHz(int freqHz)
    {
        // The core regression: reading back a tuned frequency must return
        // it exactly, including 1 Hz steps the visible box rounds away.
        // Against the old #freqinput read this returns null and fails.
        var driver = MakeReadbackDriver();
        Assert.Equal(freqHz, await TuneThenRead(driver, freqHz));
    }

    [Fact]
    public async Task OneHzSteps_AreDistinguishableOnReadback()
    {
        // Two frequencies 1 Hz apart must read back as two different
        // values -- the site's own 2-decimal display collapses them into one string.
        var driver = MakeReadbackDriver();
        var a = await TuneThenRead(driver, 14_074_500);
        var b = await TuneThenRead(driver, 14_074_499);

        Assert.Equal((14_074_500, 14_074_499), (a, b));
        Assert.Equal(1, a - b);
        // ...and this is exactly why the display can't be used for the check.
        Assert.Equal("14074.50", ((ReadbackStubPage)driver._page!).Display());
    }

    [Fact]
    public async Task Verification_ConfirmsWithoutWarningOrCorrectiveRetune()
    {
        // A successful tune must verify cleanly: no "could not read back"
        // warning, and no corrective setband/setfreq re-tune. Against the
        // old #freqinput read the readback is null, which logs that warning.
        var driver = MakeReadbackDriver();
        await driver.TuneHzAsync(14_074_499, verify: false);
        var page = (ReadbackStubPage)driver._page!;
        page.SetbandCalls.Clear();
        page.SetfreqCalls.Clear();

        var logger = new ListLogger();
        var original = CoreLog.Logger;
        CoreLog.Logger = logger;
        try
        {
            await driver.VerifyFreqAppliedAsync(14_074_499, CancellationToken.None);
        }
        finally
        {
            CoreLog.Logger = original;
        }

        Assert.DoesNotContain(logger.Records, r => r.Message.Contains("read back"));
        Assert.Empty(page.SetbandCalls);
        Assert.Empty(page.SetfreqCalls);
        Assert.Null(driver._lastTuneError);
    }

    [Fact]
    public async Task CwReadback_IsTheCarrierNotTheShiftedDisplay()
    {
        // In CW the page DISPLAYS carrier + (hi+lo)/2 but stores the
        // carrier in `freq`. Verification compares against the carrier,
        // so reading the display would be off by the CW filter centre
        // (750 Hz here -- far over FreqVerifyToleranceHz) and would trip a
        // bogus corrective re-tune on every CW tune. Guards against
        // "fixing" this by parsing the box instead.
        var driver = MakeReadbackDriver();
        await driver.SetModeAsync("CW", 500);
        await driver.TuneHzAsync(14_060_000, verify: false);
        var readback = await driver.ReadPageFreqHzAsync();
        var page = (ReadbackStubPage)driver._page!;
        var display = page.Display();
        page.SetbandCalls.Clear();
        page.SetfreqCalls.Clear();
        await driver.VerifyFreqAppliedAsync(14_060_000, CancellationToken.None);

        Assert.Equal(14_060_000, readback); // the carrier, exactly
        Assert.Equal("14059.25", display); // what the user actually sees
        Assert.True(Math.Abs((int)Math.Round(double.Parse(display, System.Globalization.CultureInfo.InvariantCulture) * 1000) - 14_060_000) > 10);
        // No corrective re-tune was triggered by the display/carrier gap.
        Assert.Empty(page.SetbandCalls);
        Assert.Empty(page.SetfreqCalls);
    }

    [Fact]
    public async Task Readback_ReturnsNullWhenPageHasNoFreqGlobal()
    {
        // A page that isn't a websdr.org page at all still degrades to
        // null rather than throwing.
        var driver = MakeReadbackDriver();
        Assert.Null(await driver.ReadPageFreqHzAsync());
    }
}
