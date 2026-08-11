using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;

namespace SDRSync.Core;

/// <summary>
/// User-editable settings, persisted as JSON in the user's home directory.
/// Ported field-for-field from sdrsync/config.py's AppSettings, including
/// its validation asymmetry: Load() must degrade gracefully on a hand-edited
/// or partially-written config-dotnet.json rather than crash or silently
/// carry a wrong-typed value.
///
/// Deliberately uses a distinct filename (config-dotnet.json, not
/// config.json) so a side-by-side Python install's config is never read or
/// clobbered by this port.
///
/// Unlike the Python original (which patches module-level CONFIG_DIR/
/// CONFIG_FILE globals for tests), Load()/Save() take the config file path
/// as a parameter -- avoids shared mutable static state, which would be
/// unsafe if xUnit ever runs test classes in parallel.
/// </summary>
public sealed class AppSettings
{
    public const double MinPollIntervalS = 0.05;
    public const double MaxPollIntervalS = 5.0;

    // Floor for the WebSDR popup's size -- kept alongside the poll-interval
    // bounds even though nothing in the Core layer enforces it yet, so a
    // later WebView-hosting layer can reference these without redefining
    // them (same split reasoning as the Python original's config.py).
    public const int MinWebviewWidth = 400;
    public const int MinWebviewHeight = 300;

    public static readonly IReadOnlySet<string> RigBackends = new HashSet<string> { "rigctld", "flrig" };

    [JsonPropertyName("rigctld_host")]
    public string RigctldHost { get; set; } = "127.0.0.1";

    [JsonPropertyName("rigctld_port")]
    public int RigctldPort { get; set; } = 4532;

    [JsonPropertyName("flrig_host")]
    public string FlrigHost { get; set; } = "127.0.0.1";

    [JsonPropertyName("flrig_port")]
    public int FlrigPort { get; set; } = 12345;

    /// <summary>Which rig-control backend is active -- "rigctld" or "flrig".</summary>
    [JsonPropertyName("rig_backend")]
    public string RigBackend { get; set; } = "rigctld";

    [JsonPropertyName("last_site_url")]
    public string LastSiteUrl { get; set; } = KnownSites.List[0].Url;

    /// <summary>
    /// Set alongside LastSiteUrl so a Custom URL site (not present in
    /// KnownSites) can be reconstructed on restart instead of silently
    /// reverting to KnownSites[0]. Empty string means "not a custom site".
    /// </summary>
    [JsonPropertyName("last_site_driver_type")]
    public string LastSiteDriverType { get; set; } = "";

    [JsonPropertyName("user_sites")]
    public List<SiteEntry> UserSites { get; set; } = new();

    [JsonPropertyName("imported_sites")]
    public List<SiteEntry> ImportedSites { get; set; } = new();

    [JsonPropertyName("curated_sites")]
    public List<SiteEntry> CuratedSites { get; set; } = new();

    [JsonPropertyName("cw_offset_hz")]
    public int CwOffsetHz { get; set; }

    [JsonPropertyName("mute_on_tx")]
    public bool MuteOnTx { get; set; } = true;

    /// <summary>
    /// Purely a display-gate today -- the strip's TX VFO readout mirrors RX
    /// VFO regardless (no real split/second-VFO rig polling exists), but
    /// when this is off the TX VFO label dims and relabels. Kept as a real,
    /// persisted setting even though it has no functional effect yet.
    /// </summary>
    [JsonPropertyName("sync_tx_vfo")]
    public bool SyncTxVfo { get; set; } = true;

    [JsonPropertyName("hide_receiver_when_undocked")]
    public bool HideReceiverWhenUndocked { get; set; }

    [JsonPropertyName("keep_compact_bar_on_top")]
    public bool KeepCompactBarOnTop { get; set; } = true;

    [JsonPropertyName("headless")]
    public bool Headless { get; set; }

    [JsonPropertyName("use_mock_rig")]
    public bool UseMockRig { get; set; }

    /// <summary>
    /// How often the sync loop polls the rig and pushes to the WebSDR, in
    /// seconds. Clamped to [MinPollIntervalS, MaxPollIntervalS] both here
    /// (a hand-edited value) and by whatever GUI control edits it, so the
    /// two layers can't drift apart.
    /// </summary>
    [JsonPropertyName("poll_interval_s")]
    public double PollIntervalS { get; set; } = 0.2;

    /// <summary>
    /// Reverse sync (WebSDR -> rig) frequency safety bound. Null means
    /// unrestricted on that side; either bound can be set independently.
    /// </summary>
    [JsonPropertyName("reverse_sync_min_hz")]
    public int? ReverseSyncMinHz { get; set; }

    [JsonPropertyName("reverse_sync_max_hz")]
    public int? ReverseSyncMaxHz { get; set; }

    /// <summary>
    /// Minutes of no rig activity after which the WebSDR session is
    /// disconnected. Null or &lt;= 0 disables it entirely. Defaults ON
    /// (unlike the reverse-sync guards): the cost of being wrong here is a
    /// few seconds of reconnect delay, while the cost of never
    /// disconnecting is borne by someone else's volunteer-run hardware.
    /// </summary>
    [JsonPropertyName("websdr_idle_disconnect_min")]
    public int? WebsdrIdleDisconnectMin { get; set; } = 60;

    /// <summary>
    /// Remembers the main window's POSITION only (not size -- the window
    /// has a fixed default/min size and is otherwise user-resizable).
    /// </summary>
    [JsonPropertyName("main_window_position")]
    public int[]? MainWindowPosition { get; set; }

    /// <summary>
    /// Set when the user dismisses the startup update-available popup for a
    /// specific version -- compared against the exact latest tag each
    /// check, not a bare bool, so dismissing one version doesn't silence a
    /// later one.
    /// </summary>
    [JsonPropertyName("dismissed_update_version")]
    public string? DismissedUpdateVersion { get; set; }

    private static readonly JsonSerializerOptions SaveOptions = new() { WriteIndented = true };

    public static string DefaultConfigDir =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".sdrsync");

    public static string DefaultConfigFile => Path.Combine(DefaultConfigDir, "config-dotnet.json");

    public static AppSettings Load() => Load(DefaultConfigFile);

    public static AppSettings Load(string configFile)
    {
        if (!File.Exists(configFile))
        {
            return new AppSettings();
        }

        JsonDocument doc;
        try
        {
            var text = File.ReadAllText(configFile, Encoding.UTF8);
            doc = JsonDocument.Parse(text);
        }
        catch (Exception e) when (e is JsonException or IOException or UnauthorizedAccessException)
        {
            CoreLog.Logger.LogWarning("Could not load {ConfigFile} ({Message}); using defaults", configFile, e.Message);
            return new AppSettings();
        }

        using (doc)
        {
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                CoreLog.Logger.LogWarning(
                    "Ignoring {ConfigFile}: expected a JSON object at the top level, got {Kind}; using defaults",
                    configFile, root.ValueKind);
                return new AppSettings();
            }

            var settings = new AppSettings
            {
                RigctldHost = GetString(root, "rigctld_host", "127.0.0.1"),
                RigctldPort = GetInt(root, "rigctld_port", 4532),
                FlrigHost = GetString(root, "flrig_host", "127.0.0.1"),
                FlrigPort = GetInt(root, "flrig_port", 12345),
                LastSiteUrl = GetString(root, "last_site_url", KnownSites.List[0].Url),
                LastSiteDriverType = GetString(root, "last_site_driver_type", ""),
                UserSites = GetSiteList(root, "user_sites", configFile),
                ImportedSites = GetSiteList(root, "imported_sites", configFile),
                CuratedSites = GetSiteList(root, "curated_sites", configFile),
                CwOffsetHz = GetInt(root, "cw_offset_hz", 0),
                MuteOnTx = GetBool(root, "mute_on_tx", true),
                SyncTxVfo = GetBool(root, "sync_tx_vfo", true),
                HideReceiverWhenUndocked = GetBool(root, "hide_receiver_when_undocked", false),
                KeepCompactBarOnTop = GetBool(root, "keep_compact_bar_on_top", true),
                Headless = GetBool(root, "headless", false),
                UseMockRig = GetBool(root, "use_mock_rig", false),
                MainWindowPosition = GetMainWindowPosition(root, configFile),
                DismissedUpdateVersion = GetNullableString(root, "dismissed_update_version", null),
            };

            // rig_backend: type-checked by GetString (falls back to default
            // on wrong type), then membership-checked separately -- mirrors
            // config.py's _validate_scalars followed by _validate_rig_backend.
            var rigBackend = GetString(root, "rig_backend", "rigctld");
            settings.RigBackend = RigBackends.Contains(rigBackend) ? rigBackend : "rigctld";

            // Clamped unconditionally into [Min,Max] -- the fallback default
            // (0.2) already sits inside that range, so clamping it too is a
            // no-op and behaviorally identical to config.py's
            // _clamp_poll_interval (which only clamps a present, validly-
            // typed value).
            settings.PollIntervalS = Math.Clamp(
                GetDouble(root, "poll_interval_s", 0.2), MinPollIntervalS, MaxPollIntervalS);

            var rawMin = GetNullableInt(root, "reverse_sync_min_hz", null);
            var rawMax = GetNullableInt(root, "reverse_sync_max_hz", null);
            (settings.ReverseSyncMinHz, settings.ReverseSyncMaxHz) = ClampReverseSyncBounds(rawMin, rawMax);

            var rawIdle = GetNullableInt(root, "websdr_idle_disconnect_min", 60);
            settings.WebsdrIdleDisconnectMin = ClampIdleDisconnectMin(rawIdle);

            return settings;
        }
    }

    /// <summary>
    /// Write-temp-then-atomic-replace so a crash mid-write can't truncate
    /// the real config file. File.Move(overwrite: true) is the atomic-
    /// rename equivalent of Python's os.replace() on both Windows and
    /// POSIX filesystems, given source and destination share a directory
    /// (guaranteed here).
    /// </summary>
    public void Save() => Save(DefaultConfigFile);

    public void Save(string configFile)
    {
        try
        {
            var dir = Path.GetDirectoryName(configFile);
            if (!string.IsNullOrEmpty(dir))
            {
                Directory.CreateDirectory(dir);
            }

            var tmpPath = configFile + ".tmp";
            var json = JsonSerializer.Serialize(this, SaveOptions);
            using (var fs = new FileStream(tmpPath, FileMode.Create, FileAccess.Write, FileShare.None))
            {
                var bytes = Encoding.UTF8.GetBytes(json);
                fs.Write(bytes, 0, bytes.Length);
                fs.Flush(true); // flushToDisk: true -- fsync equivalent
            }

            File.Move(tmpPath, configFile, overwrite: true);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            CoreLog.Logger.LogWarning("Could not save settings to {ConfigFile} ({Message})", configFile, e.Message);
        }
    }

    /// <summary>
    /// Pure sanity-check of a reverse-sync min/max Hz pair. Shared by
    /// Load() (a hand-edited config file) and any GUI range-entry field, so
    /// the two layers enforcing this safety bound can't drift apart.
    ///
    /// A negative bound can never be satisfied by a real frequency, so it
    /// would silently reject every reverse-sync push forever with no way
    /// for the user to tell why -- that one bound is dropped to null
    /// (unrestricted) rather than left as a value nothing can pass.
    ///
    /// An inverted range (min &gt; max, both otherwise valid) is SWAPPED, not
    /// dropped -- deliberately the opposite correction direction from
    /// PollIntervalS's fail-open clamping, because here loosening is the
    /// unsafe direction: this range bounds what a public WebSDR page may
    /// retune a real transmitter to. Resetting both bounds to null would
    /// silently leave no guard at all. An honest transposition is
    /// overwhelmingly the likely cause, so swapping keeps *a* range
    /// enforced instead of quietly removing the guard.
    /// </summary>
    public static (int? Min, int? Max) ClampReverseSyncBounds(int? minHz, int? maxHz)
    {
        if (minHz is < 0)
        {
            CoreLog.Logger.LogWarning("Ignoring negative reverse-sync minimum {Min}; unrestricted on that side", minHz);
            minHz = null;
        }

        if (maxHz is < 0)
        {
            CoreLog.Logger.LogWarning("Ignoring negative reverse-sync maximum {Max}; unrestricted on that side", maxHz);
            maxHz = null;
        }

        if (minHz is not null && maxHz is not null && minHz > maxHz)
        {
            CoreLog.Logger.LogWarning(
                "Reverse-sync range {Min}..{Max} is inverted -- swapping rather than dropping the guard",
                minHz, maxHz);
            (minHz, maxHz) = (maxHz, minHz);
        }

        return (minHz, maxHz);
    }

    /// <summary>
    /// Pure sanity-check of WebsdrIdleDisconnectMin. A negative value is
    /// dropped to null (feature off) -- deliberately the OPPOSITE
    /// correction direction from ClampReverseSyncBounds' "keep *a* guard":
    /// there, loosening was the unsafe direction; here the unsafe direction
    /// is the other way round -- a bad value read as "disconnect more
    /// eagerly" would tear down a working WebSDR session mid-QSO, whereas
    /// failing to idle-disconnect only restores the old, merely impolite
    /// behavior. Fail toward "off".
    /// </summary>
    public static int? ClampIdleDisconnectMin(int? value)
    {
        if (value is < 0)
        {
            CoreLog.Logger.LogWarning("Ignoring negative websdr_idle_disconnect_min {Value}; idle disconnect disabled", value);
            return null;
        }

        return value;
    }

    // --- JSON extraction helpers -------------------------------------
    //
    // Each returns the field's already-defaulted value directly: absent or
    // wrong-JSON-type both fall back to `fallback`, exactly like
    // config.py's two-step "known_fields filter, then _validate_scalars
    // drops wrong-typed values" -- collapsed into one pass here since C#'s
    // static properties already encode each field's expected type, so a
    // separate _SCALAR_TYPES map has no equivalent need. Unlike Python
    // (where bool is a subclass of int and needs an explicit exclusion),
    // System.Text.Json's JsonValueKind keeps True/False and Number
    // entirely separate, so no such special-casing is needed here.

    private static string GetString(JsonElement root, string key, string fallback)
    {
        if (root.TryGetProperty(key, out var el) && el.ValueKind == JsonValueKind.String)
        {
            return el.GetString() ?? fallback;
        }

        return fallback;
    }

    private static string? GetNullableString(JsonElement root, string key, string? fallback)
    {
        if (root.TryGetProperty(key, out var el))
        {
            if (el.ValueKind == JsonValueKind.Null) return null;
            if (el.ValueKind == JsonValueKind.String) return el.GetString();
        }

        return fallback;
    }

    private static int GetInt(JsonElement root, string key, int fallback)
    {
        if (root.TryGetProperty(key, out var el) && el.ValueKind == JsonValueKind.Number && el.TryGetInt32(out var v))
        {
            return v;
        }

        return fallback;
    }

    private static int? GetNullableInt(JsonElement root, string key, int? fallback)
    {
        if (root.TryGetProperty(key, out var el))
        {
            if (el.ValueKind == JsonValueKind.Null) return null;
            if (el.ValueKind == JsonValueKind.Number && el.TryGetInt32(out var v)) return v;
            return fallback; // present but wrong type -> default
        }

        return fallback; // absent -> default
    }

    private static double GetDouble(JsonElement root, string key, double fallback)
    {
        if (root.TryGetProperty(key, out var el) && el.ValueKind == JsonValueKind.Number && el.TryGetDouble(out var v))
        {
            return v;
        }

        return fallback;
    }

    private static bool GetBool(JsonElement root, string key, bool fallback)
    {
        if (root.TryGetProperty(key, out var el))
        {
            if (el.ValueKind == JsonValueKind.True) return true;
            if (el.ValueKind == JsonValueKind.False) return false;
        }

        return fallback;
    }

    private static int[]? GetMainWindowPosition(JsonElement root, string configFile)
    {
        if (!root.TryGetProperty("main_window_position", out var el)) return null;
        if (el.ValueKind == JsonValueKind.Null) return null;

        if (el.ValueKind == JsonValueKind.Array && el.GetArrayLength() == 2)
        {
            var items = el.EnumerateArray().ToArray();
            if (items[0].ValueKind == JsonValueKind.Number && items[1].ValueKind == JsonValueKind.Number
                && items[0].TryGetInt32(out var x) && items[1].TryGetInt32(out var y))
            {
                return new[] { x, y };
            }
        }

        CoreLog.Logger.LogWarning("Ignoring malformed main_window_position in {ConfigFile}", configFile);
        return null;
    }

    /// <summary>
    /// Drops any malformed entry in a site-list field individually rather
    /// than the whole list, mirroring config.py's _validate_site_list: a
    /// lenient, shape-only check (non-empty name/url/driver_type strings).
    /// </summary>
    private static List<SiteEntry> GetSiteList(JsonElement root, string key, string configFile)
    {
        var result = new List<SiteEntry>();
        if (!root.TryGetProperty(key, out var el)) return result;

        if (el.ValueKind != JsonValueKind.Array)
        {
            CoreLog.Logger.LogWarning("Ignoring invalid {Key} value in {ConfigFile} (expected a list)", key, configFile);
            return result;
        }

        foreach (var entry in el.EnumerateArray())
        {
            var validated = ValidateSiteEntry(entry);
            if (validated is null)
            {
                CoreLog.Logger.LogWarning("Skipping malformed {Key} entry in {ConfigFile}", key, configFile);
            }
            else
            {
                result.Add(validated);
            }
        }

        return result;
    }

    private static SiteEntry? ValidateSiteEntry(JsonElement entry)
    {
        if (entry.ValueKind != JsonValueKind.Object) return null;

        if (!entry.TryGetProperty("name", out var nameEl) || nameEl.ValueKind != JsonValueKind.String) return null;
        if (!entry.TryGetProperty("url", out var urlEl) || urlEl.ValueKind != JsonValueKind.String) return null;
        if (!entry.TryGetProperty("driver_type", out var dtEl) || dtEl.ValueKind != JsonValueKind.String) return null;

        var name = nameEl.GetString();
        var url = urlEl.GetString();
        var driverType = dtEl.GetString();
        if (string.IsNullOrEmpty(name) || string.IsNullOrEmpty(url) || string.IsNullOrEmpty(driverType))
        {
            return null;
        }

        return new SiteEntry(name, url, driverType);
    }
}
