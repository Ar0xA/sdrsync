using SDRSync.Core;
using Xunit;

namespace SDRSync.Tests;

/// <summary>
/// Ported from tests/test_config.py -- AppSettings.Load()/Save()
/// robustness against a hand-edited or partially-written config file.
/// Each test gets its own isolated temp directory (the C# analog of
/// pytest's tmp_path fixture), passed explicitly to Load()/Save() rather
/// than patched module globals -- see AppSettings' class doc comment for why.
/// </summary>
public class AppSettingsTests
{
    private static string NewConfigFile()
    {
        var dir = Path.Combine(Path.GetTempPath(), "sdrsync-tests-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        return Path.Combine(dir, "config-dotnet.json");
    }

    private static void WriteJson(string configFile, string json) => File.WriteAllText(configFile, json);

    [Fact]
    public void Load_DefaultsWhenNoFile()
    {
        var settings = AppSettings.Load(NewConfigFile());
        Assert.Equal(4532, settings.RigctldPort);
        Assert.Empty(settings.UserSites);
    }

    [Fact]
    public void Load_SkipsMalformedUserSitesEntries()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """
            {"user_sites": [
                {"name": "Good", "url": "http://good.example/", "driver_type": "kiwisdr"},
                {"name": "Missing URL", "driver_type": "kiwisdr"},
                "not even a dict",
                {"name": "", "url": "http://empty-name.example/", "driver_type": "kiwisdr"},
                null
            ]}
            """);

        var settings = AppSettings.Load(configFile);

        Assert.Equal(new[] { new SiteEntry("Good", "http://good.example/", "kiwisdr") }, settings.UserSites);
    }

    [Fact]
    public void Load_IgnoresInvalidUserSitesType()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"user_sites": "not a list"}""");

        var settings = AppSettings.Load(configFile);

        Assert.Empty(settings.UserSites);
    }

    [Fact]
    public void Load_IgnoresWrongTypeScalarAndFallsBackToDefault()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"rigctld_port": "4532", "headless": "yes"}""");

        var settings = AppSettings.Load(configFile);

        Assert.Equal(4532, settings.RigctldPort); // int default, not the string
        Assert.False(settings.Headless); // bool default, not the string
    }

    [Fact]
    public void Load_AcceptsPollIntervalAsIntOrFloat()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"poll_interval_s": 1}""");
        Assert.Equal(1.0, AppSettings.Load(configFile).PollIntervalS);

        WriteJson(configFile, """{"poll_interval_s": "0.1"}""");
        Assert.Equal(0.2, AppSettings.Load(configFile).PollIntervalS); // falls back to default, not the string
    }

    [Fact]
    public void Load_ClampsOutOfRangePollInterval()
    {
        var configFile = NewConfigFile();

        WriteJson(configFile, """{"poll_interval_s": -5}""");
        Assert.Equal(AppSettings.MinPollIntervalS, AppSettings.Load(configFile).PollIntervalS);

        WriteJson(configFile, """{"poll_interval_s": 0}""");
        Assert.Equal(AppSettings.MinPollIntervalS, AppSettings.Load(configFile).PollIntervalS);

        WriteJson(configFile, """{"poll_interval_s": 9999}""");
        Assert.Equal(AppSettings.MaxPollIntervalS, AppSettings.Load(configFile).PollIntervalS);
    }

    [Fact]
    public void Load_AcceptsValidReverseSyncRange()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"reverse_sync_min_hz": 1800000, "reverse_sync_max_hz": 30000000}""");

        var settings = AppSettings.Load(configFile);

        Assert.Equal(1_800_000, settings.ReverseSyncMinHz);
        Assert.Equal(30_000_000, settings.ReverseSyncMaxHz);
    }

    [Fact]
    public void Load_DefaultsReverseSyncRangeToUnrestricted()
    {
        var settings = AppSettings.Load(NewConfigFile());
        Assert.Null(settings.ReverseSyncMinHz);
        Assert.Null(settings.ReverseSyncMaxHz);
    }

    [Fact]
    public void Load_AcceptsReverseSyncRangeWithOnlyOneBoundSet()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"reverse_sync_min_hz": 100000}""");

        var settings = AppSettings.Load(configFile);

        Assert.Equal(100_000, settings.ReverseSyncMinHz);
        Assert.Null(settings.ReverseSyncMaxHz);
    }

    [Fact]
    public void Load_RejectsWrongTypeReverseSyncRange()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"reverse_sync_min_hz": "not a number", "reverse_sync_max_hz": true}""");

        var settings = AppSettings.Load(configFile);

        Assert.Null(settings.ReverseSyncMinHz);
        Assert.Null(settings.ReverseSyncMaxHz);
    }

    [Fact]
    public void Load_ClampsNegativeReverseSyncBoundsToUnrestricted()
    {
        var configFile = NewConfigFile();

        WriteJson(configFile, """{"reverse_sync_min_hz": -100}""");
        Assert.Null(AppSettings.Load(configFile).ReverseSyncMinHz);

        WriteJson(configFile, """{"reverse_sync_max_hz": -1}""");
        Assert.Null(AppSettings.Load(configFile).ReverseSyncMaxHz);
    }

    [Fact]
    public void ClampReverseSyncBounds_SwapsInvertedRange()
    {
        // An inverted range (min > max) is almost always a transposition of
        // the intended bounds -- must be SWAPPED, not reset to unrestricted:
        // loosening is the unsafe direction for this guard (it bounds what a
        // public WebSDR page may retune a real transmitter to).
        var (min, max) = AppSettings.ClampReverseSyncBounds(30_000_000, 1_800_000);
        Assert.Equal(1_800_000, min);
        Assert.Equal(30_000_000, max);
    }

    [Fact]
    public void Load_SwapsInvertedReverseSyncRangeEndToEnd()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"reverse_sync_min_hz": 30000000, "reverse_sync_max_hz": 1800000}""");

        var settings = AppSettings.Load(configFile);

        Assert.Equal(1_800_000, settings.ReverseSyncMinHz);
        Assert.Equal(30_000_000, settings.ReverseSyncMaxHz);
    }

    [Fact]
    public void ClampReverseSyncBounds_PassesThroughAValidRange()
    {
        Assert.Equal((1_800_000, 30_000_000), AppSettings.ClampReverseSyncBounds(1_800_000, 30_000_000));
        Assert.Equal(((int?)1_800_000, (int?)null), AppSettings.ClampReverseSyncBounds(1_800_000, null));
        Assert.Equal(((int?)null, (int?)30_000_000), AppSettings.ClampReverseSyncBounds(null, 30_000_000));
        Assert.Equal(((int?)null, (int?)null), AppSettings.ClampReverseSyncBounds(null, null));
    }

    [Fact]
    public void ClampReverseSyncBounds_AllowsAnEqualMinAndMax()
    {
        // min == max is a degenerate but legitimate single-frequency lock,
        // not an inverted range -- must pass through untouched.
        Assert.Equal((14_074_000, 14_074_000), AppSettings.ClampReverseSyncBounds(14_074_000, 14_074_000));
    }

    [Fact]
    public void ClampReverseSyncBounds_DropsOnlyTheNegativeBound()
    {
        Assert.Equal(((int?)null, (int?)30_000_000), AppSettings.ClampReverseSyncBounds(-100, 30_000_000));
        Assert.Equal(((int?)1_800_000, (int?)null), AppSettings.ClampReverseSyncBounds(1_800_000, -1));
        Assert.Equal(((int?)null, (int?)null), AppSettings.ClampReverseSyncBounds(-5, -9));
    }

    [Fact]
    public void ClampReverseSyncBounds_DoesNotSwapWhenABoundWasDropped()
    {
        // A negative min is dropped BEFORE the inversion check, so the pair
        // can't then be "swapped" into resurrecting the discarded value.
        Assert.Equal(((int?)null, (int?)1_800_000), AppSettings.ClampReverseSyncBounds(-30_000_000, 1_800_000));
    }

    [Theory]
    [InlineData("[]")]
    [InlineData("\"just a string\"")]
    [InlineData("5")]
    [InlineData("null")]
    public void Load_DoesNotCrashOnNonObjectTopLevelJson(string badTopLevel)
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, badTopLevel);

        var settings = AppSettings.Load(configFile); // must not throw

        Assert.Equal(4532, settings.RigctldPort);
    }

    [Fact]
    public void Load_DoesNotCrashOnGarbageJson()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, "{not valid json");

        var settings = AppSettings.Load(configFile); // must not throw

        Assert.Equal(4532, settings.RigctldPort);
    }

    [Fact]
    public void SaveThenLoad_RoundTripsValidData()
    {
        var configFile = NewConfigFile();
        var original = new AppSettings
        {
            RigctldPort = 4534,
            UserSites = new List<SiteEntry> { new("A", "http://a.example/", "websdr_org") },
        };

        original.Save(configFile);
        var reloaded = AppSettings.Load(configFile);

        Assert.Equal(4534, reloaded.RigctldPort);
        Assert.Equal(new[] { new SiteEntry("A", "http://a.example/", "websdr_org") }, reloaded.UserSites);
    }

    [Fact]
    public void Save_DoesNotLeaveATmpFileBehind()
    {
        var configFile = NewConfigFile();
        new AppSettings().Save(configFile);

        Assert.True(File.Exists(configFile));
        Assert.False(File.Exists(configFile + ".tmp"));
    }

    [Fact]
    public void Load_AcceptsValidRigBackend()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"rig_backend": "flrig"}""");

        Assert.Equal("flrig", AppSettings.Load(configFile).RigBackend);
    }

    [Fact]
    public void Load_RejectsInvalidRigBackend()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"rig_backend": "omnirig"}""");

        Assert.Equal("rigctld", AppSettings.Load(configFile).RigBackend); // falls back to default
    }

    [Fact]
    public void Load_IgnoresWrongTypeFlrigFields()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"flrig_host": 12345, "flrig_port": "12345"}""");

        var settings = AppSettings.Load(configFile);

        Assert.Equal("127.0.0.1", settings.FlrigHost);
        Assert.Equal(12345, settings.FlrigPort);
    }

    [Fact]
    public void SaveThenLoad_RoundTripsFlrigSettings()
    {
        var configFile = NewConfigFile();
        var original = new AppSettings { RigBackend = "flrig", FlrigHost = "192.168.1.50", FlrigPort = 12346 };

        original.Save(configFile);
        var reloaded = AppSettings.Load(configFile);

        Assert.Equal("flrig", reloaded.RigBackend);
        Assert.Equal("192.168.1.50", reloaded.FlrigHost);
        Assert.Equal(12346, reloaded.FlrigPort);
    }

    [Fact]
    public void Load_DefaultsImportedAndCuratedSitesToEmptyList()
    {
        var settings = AppSettings.Load(NewConfigFile());
        Assert.Empty(settings.ImportedSites);
        Assert.Empty(settings.CuratedSites);
    }

    [Fact]
    public void Load_SkipsMalformedImportedSitesEntries()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """
            {"imported_sites": [
                {"name": "Good", "url": "http://good.example/", "driver_type": "kiwisdr"},
                {"name": "Missing URL", "driver_type": "kiwisdr"},
                "not even a dict",
                {"name": "", "url": "http://empty-name.example/", "driver_type": "kiwisdr"},
                null
            ]}
            """);

        var settings = AppSettings.Load(configFile);

        Assert.Equal(new[] { new SiteEntry("Good", "http://good.example/", "kiwisdr") }, settings.ImportedSites);
    }

    [Fact]
    public void Load_SkipsMalformedCuratedSitesEntries()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """
            {"curated_sites": [
                {"name": "Good", "url": "http://good.example/", "driver_type": "websdr_org"},
                {"name": "Bad", "url": "", "driver_type": "websdr_org"}
            ]}
            """);

        var settings = AppSettings.Load(configFile);

        Assert.Equal(new[] { new SiteEntry("Good", "http://good.example/", "websdr_org") }, settings.CuratedSites);
    }

    [Fact]
    public void Load_IgnoresInvalidImportedAndCuratedSitesType()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"imported_sites": "not a list", "curated_sites": 5}""");

        var settings = AppSettings.Load(configFile);

        Assert.Empty(settings.ImportedSites);
        Assert.Empty(settings.CuratedSites);
    }

    [Fact]
    public void SaveThenLoad_RoundTripsImportedAndCuratedSites()
    {
        var configFile = NewConfigFile();
        var original = new AppSettings
        {
            ImportedSites = new List<SiteEntry> { new("I", "http://i.example/", "kiwisdr") },
            CuratedSites = new List<SiteEntry> { new("C", "http://c.example/", "openwebrx") },
        };

        original.Save(configFile);
        var reloaded = AppSettings.Load(configFile);

        Assert.Equal(new[] { new SiteEntry("I", "http://i.example/", "kiwisdr") }, reloaded.ImportedSites);
        Assert.Equal(new[] { new SiteEntry("C", "http://c.example/", "openwebrx") }, reloaded.CuratedSites);
    }

    // --- v14: websdr_idle_disconnect_min -----------------------------------

    [Fact]
    public void IdleDisconnect_DefaultsToSixtyMinutes()
    {
        Assert.Equal(60, AppSettings.Load(NewConfigFile()).WebsdrIdleDisconnectMin);
    }

    [Fact]
    public void Load_AcceptsAValidIdleDisconnect()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"websdr_idle_disconnect_min": 15}""");
        Assert.Equal(15, AppSettings.Load(configFile).WebsdrIdleDisconnectMin);
    }

    [Fact]
    public void Load_AcceptsNullIdleDisconnectAsDisabled()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"websdr_idle_disconnect_min": null}""");
        Assert.Null(AppSettings.Load(configFile).WebsdrIdleDisconnectMin);
    }

    [Fact]
    public void Load_KeepsZeroIdleDisconnectAsDisabled()
    {
        // 0 is a valid way to say "never" -- it must survive Load()
        // unchanged (the engine treats <= 0 as off), not be corrected to
        // the default.
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"websdr_idle_disconnect_min": 0}""");
        Assert.Equal(0, AppSettings.Load(configFile).WebsdrIdleDisconnectMin);
    }

    [Fact]
    public void Load_RejectsWrongTypeIdleDisconnect()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"websdr_idle_disconnect_min": "60"}""");
        Assert.Equal(60, AppSettings.Load(configFile).WebsdrIdleDisconnectMin); // falls back to the default
    }

    [Fact]
    public void Load_ClampsNegativeIdleDisconnectToDisabled()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"websdr_idle_disconnect_min": -5}""");
        Assert.Null(AppSettings.Load(configFile).WebsdrIdleDisconnectMin);
    }

    // --- main-window geometry memory: main_window_position ------------------

    [Fact]
    public void MainWindowPosition_DefaultsToNull()
    {
        Assert.Null(AppSettings.Load(NewConfigFile()).MainWindowPosition);
    }

    [Fact]
    public void Load_AcceptsValidMainWindowPosition()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"main_window_position": [-1200, 40]}""");
        Assert.Equal(new[] { -1200, 40 }, AppSettings.Load(configFile).MainWindowPosition);
    }

    [Fact]
    public void Load_RejectsMalformedMainWindowPosition()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"main_window_position": "not a list"}""");
        Assert.Null(AppSettings.Load(configFile).MainWindowPosition);
    }

    [Fact]
    public void ClampIdleDisconnectMin_PassesThroughValidValues()
    {
        Assert.Equal(30, AppSettings.ClampIdleDisconnectMin(30));
        Assert.Equal(0, AppSettings.ClampIdleDisconnectMin(0));
        Assert.Null(AppSettings.ClampIdleDisconnectMin(null));
    }

    [Fact]
    public void SaveThenLoad_RoundTripsIdleDisconnect()
    {
        var configFile = NewConfigFile();
        new AppSettings { WebsdrIdleDisconnectMin = 45 }.Save(configFile);
        Assert.Equal(45, AppSettings.Load(configFile).WebsdrIdleDisconnectMin);
    }

    [Fact]
    public void DismissedUpdateVersion_DefaultsToNull()
    {
        Assert.Null(AppSettings.Load(NewConfigFile()).DismissedUpdateVersion);
    }

    [Fact]
    public void Load_AcceptsValidDismissedUpdateVersion()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"dismissed_update_version": "v2.2.0"}""");
        Assert.Equal("v2.2.0", AppSettings.Load(configFile).DismissedUpdateVersion);
    }

    [Fact]
    public void Load_RejectsWrongTypeDismissedUpdateVersion()
    {
        var configFile = NewConfigFile();
        WriteJson(configFile, """{"dismissed_update_version": 210}""");
        Assert.Null(AppSettings.Load(configFile).DismissedUpdateVersion);
    }

    [Fact]
    public void SaveThenLoad_RoundTripsDismissedUpdateVersion()
    {
        var configFile = NewConfigFile();
        new AppSettings { DismissedUpdateVersion = "v2.2.0" }.Save(configFile);
        Assert.Equal("v2.2.0", AppSettings.Load(configFile).DismissedUpdateVersion);
    }
}
