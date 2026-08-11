using System.Text.RegularExpressions;

namespace SDRSync.WebSdr;

/// <summary>
/// Maps a SiteEntry.DriverType string to its driver factory, and guesses a
/// driver_type from a WebSDR site's raw HTML -- ported from
/// sdrsync/websdr/registry.py.
///
/// Add a new WebSDR software family by implementing IWebSDRDriver in its
/// own file (including a public static FingerprintMarkers array) and
/// registering both here.
///
/// Deliberate divergence from Python: registry.py's DRIVERS dict stores
/// each driver CLASS directly and introspects driver_cls.FINGERPRINT_MARKERS
/// via getattr() at detection time. WebSDRDriverRegistry.Drivers (built in
/// step 4, ahead of any concrete driver existing) stores a
/// WebSDRDriverFactory delegate instead, not a type -- required so
/// SDRSync.Sync can construct a driver without depending on concrete
/// driver classes. That means fingerprint markers can't be recovered from
/// the factory dict the way Python recovers them from the class dict, so
/// DetectDriverType keeps its own explicit driver_type -> markers table,
/// built from each driver's own public FingerprintMarkers array (the
/// "declared alongside its control logic" source of truth is unchanged;
/// only how detection re-reads it differs).
/// </summary>
public static class WebSdrRegistry
{
    private static readonly Dictionary<string, string[]> FingerprintsByDriverType = new()
    {
        ["websdr_org"] = WebsdrOrgDriver.FingerprintMarkers,
        ["kiwisdr"] = KiwiSDRDriver.FingerprintMarkers,
        ["openwebrx"] = OpenWebRXDriver.FingerprintMarkers,
        ["ubersdr"] = UberSDRDriver.FingerprintMarkers,
    };

    /// <summary>Populates WebSDRDriverRegistry.Drivers with the four built-in drivers. Called once from the GUI composition root at startup (not auto-run -- an explicit call is clearer than a static-constructor side effect, and keeps engine/driver unit tests free to populate only the entries a given test needs).</summary>
    public static void RegisterBuiltinDrivers()
    {
        WebSDRDriverRegistry.Drivers["websdr_org"] = (url, cwOffsetHz) => new WebsdrOrgDriver(url, cwOffsetHz);
        WebSDRDriverRegistry.Drivers["kiwisdr"] = (url, cwOffsetHz) => new KiwiSDRDriver(url, cwOffsetHz);
        WebSDRDriverRegistry.Drivers["openwebrx"] = (url, cwOffsetHz) => new OpenWebRXDriver(url, cwOffsetHz);
        WebSDRDriverRegistry.Drivers["ubersdr"] = (url, cwOffsetHz) => new UberSDRDriver(url, cwOffsetHz);
    }

    private static readonly Regex ScriptSrcRe = new(
        @"<script[^>]*\bsrc\s*=\s*[""']([^""']+)[""']",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);

    /// <summary>
    /// Guess a driver_type from a WebSDR site's raw root-page HTML. Scoped
    /// to &lt;script src="..."&gt; attribute values only, not the whole
    /// HTML body -- station-description text or chat could otherwise
    /// mention a driver's filename and produce a false positive. If more
    /// than one registered driver's markers match, returns null
    /// ("ambiguous, refuse to guess") rather than picking one -- iteration
    /// order must never become a load-bearing tie-break rule.
    /// </summary>
    public static string? DetectDriverType(string html)
    {
        var scriptSrcs = ScriptSrcRe.Matches(html).Select(m => m.Groups[1].Value).ToList();
        var matches = FingerprintsByDriverType
            .Where(kv => kv.Value.Any(marker => scriptSrcs.Any(src => src.Contains(marker))))
            .Select(kv => kv.Key)
            .ToList();
        return matches.Count == 1 ? matches[0] : null;
    }
}
