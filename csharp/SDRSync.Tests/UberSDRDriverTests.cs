using System.Text.Json;
using SDRSync.WebSdr;
using Xunit;

namespace SDRSync.Tests;

/// <summary>Ported from tests/test_ubersdr_mode_mapping.py, test_ubersdr_url.py, test_ubersdr_driver.py.</summary>
public class UberSDRDriverTests
{
    // ------------------------------------------------------------------ mode mapping

    [Fact]
    public void SsbModes_MapStraightAcross()
    {
        Assert.Equal("usb", UberSDRDriver.MapHamlibModeUbersdr("USB"));
        Assert.Equal("lsb", UberSDRDriver.MapHamlibModeUbersdr("LSB"));
    }

    [Fact]
    public void ModeNames_AreCaseInsensitive()
    {
        Assert.Equal("usb", UberSDRDriver.MapHamlibModeUbersdr("usb"));
        Assert.Equal("lsb", UberSDRDriver.MapHamlibModeUbersdr("Lsb"));
    }

    [Fact]
    public void DataModes_MapToTheirSideband()
    {
        Assert.Equal("usb", UberSDRDriver.MapHamlibModeUbersdr("PKTUSB"));
        Assert.Equal("usb", UberSDRDriver.MapHamlibModeUbersdr("DATA-U"));
        Assert.Equal("lsb", UberSDRDriver.MapHamlibModeUbersdr("PKTLSB"));
        Assert.Equal("lsb", UberSDRDriver.MapHamlibModeUbersdr("DATA-L"));
    }

    [Fact]
    public void Cw_KeepsItsSidebandRatherThanCollapsing()
    {
        // UberSDR has both cwu/cwl, and the letter says which sideband the
        // tone is on. Mapping CWR onto cwu would put the tone on the wrong
        // side of the carrier.
        Assert.Equal("cwu", UberSDRDriver.MapHamlibModeUbersdr("CW"));
        Assert.Equal("cwl", UberSDRDriver.MapHamlibModeUbersdr("CWR"));
        Assert.Equal("cwu", UberSDRDriver.MapHamlibModeUbersdr("CW-U"));
        Assert.Equal("cwl", UberSDRDriver.MapHamlibModeUbersdr("CW-L"));
    }

    [Fact]
    public void AmAndSynchronousAm_AreDifferentModes()
    {
        Assert.Equal("am", UberSDRDriver.MapHamlibModeUbersdr("AM"));
        Assert.Equal("sam", UberSDRDriver.MapHamlibModeUbersdr("AMS"));
        Assert.Equal("sam", UberSDRDriver.MapHamlibModeUbersdr("SAM"));
    }

    [Fact]
    public void FmNarrowAndWide_AreDifferentModes()
    {
        Assert.Equal("nfm", UberSDRDriver.MapHamlibModeUbersdr("FM"));
        Assert.Equal("fm", UberSDRDriver.MapHamlibModeUbersdr("WFM"));
    }

    [Fact]
    public void UnknownMode_IsNullRatherThanAGuess()
    {
        Assert.Null(UberSDRDriver.MapHamlibModeUbersdr("DRM"));
        Assert.Null(UberSDRDriver.MapHamlibModeUbersdr(""));
    }

    [Fact]
    public void UsbWidth_IsAddedAboveTheModesOwnLowEdge()
    {
        // 50 Hz, not 0: the receiver's own default keeps the filter off
        // the carrier.
        Assert.Equal((50, 2450), UberSDRDriver.PassbandEdges("usb", 2400));
    }

    [Fact]
    public void Lsb_IsTheMirrorOfUsb() => Assert.Equal((-2450, -50), UberSDRDriver.PassbandEdges("lsb", 2400));

    [Fact]
    public void SymmetricModes_AreSplitEitherSideOfTheDial()
    {
        Assert.Equal((-250, 250), UberSDRDriver.PassbandEdges("cwu", 500));
        Assert.Equal((-250, 250), UberSDRDriver.PassbandEdges("cwl", 500));
        Assert.Equal((-3000, 3000), UberSDRDriver.PassbandEdges("am", 6000));
    }

    [Fact]
    public void NoWidth_MeansTheModeDecides()
    {
        Assert.Null(UberSDRDriver.PassbandEdges("usb", null));
        Assert.Null(UberSDRDriver.PassbandEdges("usb", 0));
        Assert.Null(UberSDRDriver.PassbandEdges("usb", -100));
    }

    [Fact]
    public void AWidthWiderThanTheReceiverAllows_BecomesTheWidestItHas()
    {
        var edges = UberSDRDriver.PassbandEdges("fm", 40000);
        Assert.Equal((-12000, 12000), edges);
    }

    [Fact]
    public void AWidthTooSmallToBeAFilter_IsLeftToTheMode() => Assert.Null(UberSDRDriver.PassbandEdges("usb", 1));

    [Fact]
    public void AnUnknownMode_HasNoEdges() => Assert.Null(UberSDRDriver.PassbandEdges("drm", 2400));

    [Fact]
    public void TheReceiversOwnTable_IsUsedWhenGiven()
    {
        var live = new Dictionary<string, UberSDRDriver.ModeInfo>
        {
            ["usb"] = new UberSDRDriver.ModeInfo(100, 2800, 0, 3000, "upper"),
        };
        Assert.Equal((100, 2500), UberSDRDriver.PassbandEdges("usb", 2400, live));
        // And its limits win over the built-in ones.
        Assert.Equal((100, 3000), UberSDRDriver.PassbandEdges("usb", 5000, live));
    }

    // ------------------------------------------------------------------ v2 URL resolution

    [Fact]
    public void ARootUrl_BecomesTheV2Interface()
    {
        Assert.Equal("https://rx.example/v2/", UberSDRDriver.V2PageUrl("https://rx.example/"));
        Assert.Equal("https://rx.example/v2/", UberSDRDriver.V2PageUrl("https://rx.example"));
    }

    [Fact]
    public void AUrlAlreadyPointingAtV2_IsLeftAlone() => Assert.Equal("https://rx.example/v2/", UberSDRDriver.V2PageUrl("https://rx.example/v2/"));

    [Fact]
    public void AV2UrlWithoutItsTrailingSlash_GetsOne() =>
        // Not cosmetic: relative script and asset paths on that page
        // resolve against the directory, and without the slash the
        // browser resolves them one level up.
        Assert.Equal("https://rx.example/v2/", UberSDRDriver.V2PageUrl("https://rx.example/v2"));

    [Fact]
    public void APortAndAPathPrefix_BothSurvive()
    {
        Assert.Equal("http://rx.example:8073/v2/", UberSDRDriver.V2PageUrl("http://rx.example:8073/"));
        Assert.Equal("https://host/receiver/v2/", UberSDRDriver.V2PageUrl("https://host/receiver/"));
    }

    [Fact]
    public void AShareLink_KeepsItsQuery() =>
        Assert.Equal("https://rx.example/v2/?freq=7100000&mode=lsb", UberSDRDriver.V2PageUrl("https://rx.example/?freq=7100000&mode=lsb"));

    [Fact]
    public void ADeeperV2Url_IsNotRewritten() =>
        Assert.Equal("https://rx.example/v2/index.html", UberSDRDriver.V2PageUrl("https://rx.example/v2/index.html"));

    [Fact]
    public void ABareHost_GetsAScheme() => Assert.Equal("http://rx.example/v2/", UberSDRDriver.V2PageUrl("rx.example"));

    [Fact]
    public void TheDriver_ResolvesItAtConstruction() =>
        // So every log line, error message and navigation in the driver
        // names the page it is actually driving rather than the one it
        // was handed.
        Assert.Equal("https://rx.example/v2/", new UberSDRDriver("https://rx.example/").Url);

    // ------------------------------------------------------------------ driver behavior
    // The stub stands in for the browser page. It matches on the JS the
    // driver evaluates -- white-box, like the other drivers' tests,
    // because the alternative is a real browser.

    private sealed class StubPage : StubPageBase
    {
        public Dictionary<string, object> Replies { get; }
        public Dictionary<string, object> Topics { get; }
        public List<(string Type, Dictionary<string, object?> Payload)> Sent { get; } = new();
        public bool AgentPresent { get; set; } = true;
        public bool Ready { get; set; } = true;
        public bool Closed { get; set; }

        public StubPage(Dictionary<string, object>? replies = null, Dictionary<string, object>? topics = null)
        {
            Replies = replies ?? new Dictionary<string, object>();
            Topics = topics ?? new Dictionary<string, object>();
        }

        public override Task<JsonElement?> EvaluateAsync(string js, params object?[] args)
        {
            if (js.Contains(".call("))
            {
                var fields = (Dictionary<string, object?>)args[0]!;
                var msgType = (string)fields["type"]!;
                var payload = (Dictionary<string, object?>)fields["fields"]!;
                Sent.Add((msgType, payload));
                var name = payload.TryGetValue("name", out var n) ? n as string : null;
                object reply = new Dictionary<string, object> { ["ok"] = true, ["value"] = new Dictionary<string, object>() };
                if (name is not null && Replies.TryGetValue(name, out var r))
                {
                    if (r is List<object> list)
                    {
                        reply = list.Count > 0 ? list[0] : new Dictionary<string, object> { ["ok"] = true, ["value"] = new Dictionary<string, object>() };
                        if (list.Count > 0) list.RemoveAt(0);
                    }
                    else
                    {
                        reply = r;
                    }
                }

                var envelope = JsonSerializer.SerializeToElement(new { id = Sent.Count, done = true, res = reply });
                return Task.FromResult<JsonElement?>(envelope);
            }

            if (js.Contains("topics:"))
            {
                if (!AgentPresent) return Task.FromResult<JsonElement?>(null);
                var status = new { ready = Ready, closed = Closed, refused = (string?)null, topics = Topics };
                return Task.FromResult<JsonElement?>(JsonSerializer.SerializeToElement(status));
            }

            if (js.Contains(".topics["))
            {
                var name = js.Split('\'')[1];
                return Task.FromResult<JsonElement?>(Topics.TryGetValue(name, out var t) ? JsonSerializer.SerializeToElement(t) : null);
            }

            return Task.FromResult<JsonElement?>(JsonSerializer.SerializeToElement(true));
        }
    }

    private static UberSDRDriver Attached(StubPage page, int cwOffsetHz = 0)
    {
        var driver = new UberSDRDriver("https://rx.example/", cwOffsetHz);
        driver._page = page;
        driver._attached = true;
        return driver;
    }

    // --- PTT silence ----------------------------------------------------------

    [Fact]
    public async Task PttSilence_UsesDuckAndNeverTouchesTheOperatorsMute()
    {
        // The distinction the API draws, and the reason it exists: `mute`
        // is the operator's own setting, remembered by the browser view
        // and shown on its mute button. Using it for transmit would leave
        // that mute behind to undo if sdrsync died mid-transmission --
        // `duck` leaves nothing.
        var page = new StubPage();
        var driver = Attached(page);
        await driver.SetMutedAsync(true);
        Assert.Equal("command", page.Sent[0].Type);
        Assert.Equal("duck", page.Sent[0].Payload["name"]);
        Assert.Equal(true, ((Dictionary<string, object?>)page.Sent[0].Payload["args"]!)["ducked"]);

        await driver.SetMutedAsync(false);
        Assert.Equal(false, ((Dictionary<string, object?>)page.Sent[^1].Payload["args"]!)["ducked"]);
    }

    [Fact]
    public async Task AReceiverWithoutDuck_StillGoesQuietForTransmit()
    {
        // Feature-detected on the announce's capability list. A receiver
        // without a transient-silence command still has to be silent
        // while the rig is transmitting -- RX audio over your own
        // transmission is the worse failure, so the operator's own mute is
        // used and said out loud once.
        var page = new StubPage();
        var driver = Attached(page);
        driver._canDuck = false;
        await driver.SetMutedAsync(true);
        Assert.Equal("mute", page.Sent[0].Payload["name"]);
        Assert.Equal(true, ((Dictionary<string, object?>)page.Sent[0].Payload["args"]!)["muted"]);
    }

    [Fact]
    public async Task Duck_IsUsedWhenTheReceiverReportsIt()
    {
        var page = new StubPage();
        var driver = Attached(page);
        driver._canDuck = true;
        await driver.SetMutedAsync(true);
        Assert.Equal("duck", page.Sent[^1].Payload["name"]);
    }

    [Fact]
    public async Task Silence_IsAbsoluteRatherThanAToggle()
    {
        // PTT arrives as "transmitting: true/false". A toggle
        // desynchronises permanently the first time a message is missed.
        var page = new StubPage();
        var driver = Attached(page);
        await driver.SetMutedAsync(true);
        await driver.SetMutedAsync(true);
        Assert.All(page.Sent, s => Assert.Equal(true, ((Dictionary<string, object?>)s.Payload["args"]!)["ducked"]));
    }

    // --- tuning -----------------------------------------------------------

    [Fact]
    public async Task Tune_SendsAnAbsoluteFrequencyAndAsksForTheViewToFollow()
    {
        var page = new StubPage();
        var driver = Attached(page);
        Assert.True(await driver.TuneHzAsync(14074000));
        Assert.Single(page.Sent);
        Assert.Equal("tune", page.Sent[0].Payload["name"]);
        var args = (Dictionary<string, object?>)page.Sent[0].Payload["args"]!;
        Assert.Equal(14074000, args["frequency"]);
        Assert.Equal(true, args["ensureVisible"]);
    }

    [Fact]
    public async Task TheCwOffset_AppliesOnlyInCw()
    {
        var page = new StubPage();
        var driver = Attached(page, cwOffsetHz: 700);
        await driver.TuneHzAsync(7030000);
        Assert.Equal(7030000, ((Dictionary<string, object?>)page.Sent[^1].Payload["args"]!)["frequency"]); // not CW yet
        driver._currentMode = "cwu";
        await driver.TuneHzAsync(7030000);
        Assert.Equal(7030700, ((Dictionary<string, object?>)page.Sent[^1].Payload["args"]!)["frequency"]);
    }

    [Fact]
    public async Task ARefusedFrequency_IsReportedAsNotApplied()
    {
        // The engine only latches "sent" when the driver says it applied
        // -- otherwise it would never retry once the receiver could take it.
        var page = new StubPage(replies: new Dictionary<string, object>
        {
            ["tune"] = new Dictionary<string, object>
            {
                ["ok"] = false,
                ["error"] = new Dictionary<string, object> { ["code"] = "bad_args", ["message"] = "frequency 40000000 is outside 10000-30000000" },
            },
        });
        var driver = Attached(page);
        Assert.False(await driver.TuneHzAsync(40000000));
        var status = await driver.GetStatusAsync();
        Assert.Contains("outside", status.LastError ?? ""); // the receiver's own reason is kept
    }

    [Fact]
    public async Task NothingIsSent_WhileUnattached()
    {
        var page = new StubPage();
        var driver = new UberSDRDriver("https://rx.example/") { _page = page };
        Assert.False(await driver.TuneHzAsync(14074000));
        Assert.False(await driver.SetModeAsync("USB", 2400));
        await driver.SetMutedAsync(true);
        Assert.Empty(page.Sent);
    }

    // --- mode and filter ---------------------------------------------------

    [Fact]
    public async Task ModeAndFilter_GoInOneCommand()
    {
        // Separately, the receiver passes audibly through the new mode's
        // default width on its way to the one asked for.
        var page = new StubPage();
        var driver = Attached(page);
        Assert.True(await driver.SetModeAsync("USB", 2400));
        Assert.Single(page.Sent);
        Assert.Equal("tune", page.Sent[0].Payload["name"]);
        var args = (Dictionary<string, object?>)page.Sent[0].Payload["args"]!;
        Assert.Equal("usb", args["mode"]);
        Assert.Equal(50, args["bandwidthLow"]);
        Assert.Equal(2450, args["bandwidthHigh"]);
    }

    [Fact]
    public async Task AModeWithNoReportedFilter_SaysNothingAboutTheFilter()
    {
        var page = new StubPage();
        var driver = Attached(page);
        Assert.True(await driver.SetModeAsync("LSB", null));
        var args = (Dictionary<string, object?>)page.Sent[^1].Payload["args"]!;
        Assert.Single(args);
        Assert.Equal("lsb", args["mode"]);
    }

    [Fact]
    public async Task AnUnmappedMode_IsSkippedWithoutSendingAnything()
    {
        var page = new StubPage();
        var driver = Attached(page);
        Assert.False(await driver.SetModeAsync("DRM", 6000));
        Assert.Empty(page.Sent);
        var status = await driver.GetStatusAsync();
        Assert.Contains("no UberSDR equivalent", status.LastError ?? "");
    }

    [Fact]
    public async Task ARefusedFilter_DoesNotCostUsTheModeChange()
    {
        // A receiver whose limits are tighter than we guessed refuses the
        // passband. The mode is the more important half of the request, so
        // it is retried alone rather than the whole thing being reported
        // as failed.
        var page = new StubPage(replies: new Dictionary<string, object>
        {
            ["tune"] = new List<object>
            {
                new Dictionary<string, object> { ["ok"] = false, ["error"] = new Dictionary<string, object> { ["code"] = "bad_args", ["message"] = "passband too wide for usb" } },
                new Dictionary<string, object> { ["ok"] = true, ["value"] = new Dictionary<string, object> { ["mode"] = "usb" } },
            },
        });
        var driver = Attached(page);
        Assert.True(await driver.SetModeAsync("USB", 5900));
        Assert.Equal(2, page.Sent.Count);
        Assert.True(((Dictionary<string, object?>)page.Sent[0].Payload["args"]!).ContainsKey("bandwidthLow"));
        var retryArgs = (Dictionary<string, object?>)page.Sent[1].Payload["args"]!;
        Assert.Single(retryArgs);
        Assert.Equal("usb", retryArgs["mode"]); // the retry is the mode alone
        Assert.Null((await driver.GetStatusAsync()).LastError);
    }

    [Fact]
    public async Task ARefusedMode_IsReportedAsNotApplied()
    {
        var page = new StubPage(replies: new Dictionary<string, object>
        {
            ["tune"] = new Dictionary<string, object> { ["ok"] = false, ["error"] = new Dictionary<string, object> { ["code"] = "bad_args", ["message"] = "no mode \"usb\"" } },
        });
        var driver = Attached(page);
        Assert.False(await driver.SetModeAsync("USB", null));
    }

    // --- status -------------------------------------------------------------

    [Fact]
    public async Task Status_ComesFromTheSubscribedTopics()
    {
        var page = new StubPage(topics: new Dictionary<string, object>
        {
            ["tuning"] = new Dictionary<string, object> { ["frequency"] = 14074000, ["mode"] = "usb" },
            ["session"] = new Dictionary<string, object> { ["running"] = true },
            ["audio"] = new Dictionary<string, object> { ["ducked"] = false },
        });
        var status = await Attached(page).GetStatusAsync();
        Assert.True(status.Connected);
        Assert.Equal(14074000, status.FreqHz);
        Assert.Equal("USB", status.Mode);
        Assert.True(status.AudioActive);
    }

    [Fact]
    public async Task ADuckedReceiver_IsNotReportedAsPlaying()
    {
        // It is silent, and saying otherwise is a lie the user can hear --
        // this is the state sdrsync itself puts the receiver in while the
        // rig transmits.
        var page = new StubPage(topics: new Dictionary<string, object>
        {
            ["tuning"] = new Dictionary<string, object> { ["frequency"] = 14074000, ["mode"] = "usb" },
            ["session"] = new Dictionary<string, object> { ["running"] = true },
            ["audio"] = new Dictionary<string, object> { ["ducked"] = true },
        });
        Assert.False((await Attached(page).GetStatusAsync()).AudioActive);
    }

    [Fact]
    public async Task APageThatReloaded_DropsAttachmentSoTheEngineReAttaches()
    {
        // The agent goes with the page. Continuing to send would be
        // driving a page that is no longer listening, and every command
        // would look like it worked.
        var page = new StubPage { AgentPresent = false };
        var driver = Attached(page);
        var status = await driver.GetStatusAsync();
        Assert.False(status.Connected);
        Assert.False(driver.Attached);
        Assert.Contains("reloaded", status.LastError ?? "");
    }

    [Fact]
    public async Task ThePageClosingTheConnection_AlsoDropsAttachment()
    {
        var page = new StubPage(topics: new Dictionary<string, object> { ["tuning"] = new Dictionary<string, object>() }) { Closed = true };
        var driver = Attached(page);
        Assert.False((await driver.GetStatusAsync()).Connected);
        Assert.False(driver.Attached);
    }

    [Fact]
    public async Task StatusWhileUnattached_CarriesTheReasonRatherThanBeingBlank()
    {
        var driver = new UberSDRDriver("https://rx.example/");
        driver._lastAttachError = "the browser bridge is switched off";
        var status = await driver.GetStatusAsync();
        Assert.False(status.Connected);
        Assert.Equal("the browser bridge is switched off", status.LastError);
    }

    // --- reverse sync (WebSDR -> rig, v11) -------------------------------------

    [Fact]
    public async Task ReverseSync_MapsStatusModeBackToHamlib()
    {
        var driver = Attached(new StubPage());
        var status = await driver.GetStatusAsync(); // unattached-shaped is fine; only .Mode matters
        foreach (var (ubersdrMode, hamlibMode) in new[]
                 {
                     ("USB", "USB"), ("LSB", "LSB"), ("AM", "AM"), ("SAM", "SAM"),
                     ("NFM", "FM"), ("FM", "WFM"), ("CWU", "CW"), ("CWL", "CWR"),
                 })
        {
            status.Mode = ubersdrMode;
            Assert.Equal(hamlibMode, driver.HamlibModeFromStatus(status));
        }
    }

    [Fact]
    public async Task ReverseSync_ReportsNullForAnUnmappedMode()
    {
        var driver = Attached(new StubPage());
        var status = await driver.GetStatusAsync();
        status.Mode = "DRM";
        Assert.Null(driver.HamlibModeFromStatus(status));
    }

    [Fact]
    public async Task ReverseSync_UnAppliesTheCwOffset()
    {
        // Symmetric to TheCwOffset_AppliesOnlyInCw above: the same offset
        // that gets added going out to the receiver must come back off
        // going the other way, or every reverse-synced CW frequency would drift.
        var driver = Attached(new StubPage(), cwOffsetHz: 700);
        var status = await driver.GetStatusAsync();
        status.FreqHz = 7030700;
        status.Mode = "CWU";
        Assert.Equal(7030000, driver.RigFreqFromStatus(status));
        status.Mode = "CWL";
        Assert.Equal(7030000, driver.RigFreqFromStatus(status));
    }

    [Fact]
    public async Task ReverseSync_LeavesNonCwFrequencyUntouched()
    {
        var driver = Attached(new StubPage(), cwOffsetHz: 700);
        var status = await driver.GetStatusAsync();
        status.FreqHz = 14074000;
        status.Mode = "USB";
        Assert.Equal(14074000, driver.RigFreqFromStatus(status));
    }

    [Fact]
    public async Task ReverseSync_ReadsTheDriverOwnOffsetNotTheLastPushedMode()
    {
        // RigFreqFromStatus() must key off the OBSERVED mode in this
        // status snapshot, not _currentMode (the driver's own last-pushed
        // mode) -- otherwise a receiver retuned by someone else on the
        // page (exactly what reverse sync exists to observe) would apply
        // the wrong offset.
        var driver = Attached(new StubPage(), cwOffsetHz: 700);
        driver._currentMode = "usb";
        var status = await driver.GetStatusAsync();
        status.FreqHz = 7030700;
        status.Mode = "CWU";
        Assert.Equal(7030000, driver.RigFreqFromStatus(status));
    }

    [Fact]
    public async Task ReverseSync_FrequencyIsNullWhenStatusHasNull()
    {
        var driver = Attached(new StubPage());
        var status = await driver.GetStatusAsync();
        status.FreqHz = null;
        status.Mode = "USB";
        Assert.Null(driver.RigFreqFromStatus(status));
    }

    // --- goodbye --------------------------------------------------------------

    [Fact]
    public async Task Close_SaysGoodbyeSoTheClientSlotIsFreed()
    {
        // The page holds at most eight clients and evicts the stalest to
        // make room. A session that reconnects repeatedly without saying
        // bye would push out somebody's browser extension.
        var page = new StubPage();
        var driver = Attached(page);
        await driver.CloseAsync();
        Assert.False(driver.Attached);
    }

    [Fact]
    public async Task TheReportedJson_IsWhatThePageWouldReceive()
    {
        // The driver's fields are handed to the page as JSON. Anything
        // unserialisable would fail inside the browser shim rather than
        // here, so it is checked here.
        var page = new StubPage();
        var driver = Attached(page);
        await driver.TuneHzAsync(14074000);
        await driver.SetModeAsync("CW", 500);
        foreach (var (_, payload) in page.Sent)
        {
            JsonSerializer.Serialize(payload); // must not throw
        }
    }
}
