namespace SDRSync.Core;

/// <summary>
/// Built-in starter list for the site dropdown, ported verbatim from
/// sdrsync/config.py's KNOWN_SITES. These are third-party public
/// receivers, not ours -- they can go offline, move, or develop
/// instance-specific quirks; treat this list as needing occasional
/// revalidation, not a permanent fixture (same caveat as the Python
/// source).
/// </summary>
public static class KnownSites
{
    public static readonly IReadOnlyList<SiteEntry> List = new[]
    {
        new SiteEntry(
            Name: "Twente wide-band (websdr.org)",
            Url: "http://websdr.ewi.utwente.nl:8901/",
            DriverType: "websdr_org"),
        new SiteEntry(
            Name: "KiwiSDR example (VK5ARG)",
            Url: "http://kiwisdr.areg.org.au:8073/",
            DriverType: "kiwisdr"),
        new SiteEntry(
            // 80m HF band (~2.65-4.65 MHz), confirmed live -- true full-HF
            // (0-30 MHz) single-profile OpenWebRX instances are uncommon, so
            // this is a deliberately narrower but actually-useful-for-HF
            // default rather than a wideband one like the two entries above.
            Name: "OpenWebRX example (OH6KK, 80m)",
            Url: "http://oh6kk.dy.fi:8073/",
            DriverType: "openwebrx"),
        new SiteEntry(
            // UberSDR exposes a documented control API rather than requiring
            // driving page internals -- see the WebSdr project's UberSDR
            // driver once it exists (SDRSync.WebSdr). The root URL is what
            // an operator publishes; the driver navigates to the v2
            // interface itself, which is where the API lives.
            Name: "UberSDR example (M9PSY, full HF)",
            Url: "https://m9psy.tunnel.ubersdr.org/",
            DriverType: "ubersdr"),
    };

    public static SiteEntry? FindByUrl(string url) => List.FirstOrDefault(s => s.Url == url);
}
