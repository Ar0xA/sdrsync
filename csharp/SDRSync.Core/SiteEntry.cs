using System.Text.Json.Serialization;

namespace SDRSync.Core;

/// <summary>
/// A selectable WebSDR instance and which driver knows how to control it.
/// Ported from sdrsync/config.py's WebSDRSite (the built-in KNOWN_SITES
/// list) and the identically-shaped {"name","url","driver_type"} dicts
/// used for AppSettings.UserSites/ImportedSites/CuratedSites -- the Python
/// original keeps those as two different representations (a frozen
/// dataclass vs. plain dicts) only because KNOWN_SITES predates the
/// user-editable lists; there's no behavioral reason to keep them separate
/// here, so this one immutable record serves both roles.
///
/// JsonPropertyName attributes match AppSettings.GetSiteList's hand-rolled
/// parser, which looks for the same snake_case keys the Python config.json
/// format uses -- without them System.Text.Json's default serializer emits
/// PascalCase ("DriverType"), which the parser wouldn't recognize on the
/// next Load(), silently dropping every saved site.
/// </summary>
public sealed record SiteEntry(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("url")] string Url,
    [property: JsonPropertyName("driver_type")] string DriverType);
