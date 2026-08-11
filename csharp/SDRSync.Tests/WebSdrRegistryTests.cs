using SDRSync.WebSdr;
using Xunit;

namespace SDRSync.Tests;

/// <summary>Ported from tests/test_fingerprint.py.</summary>
[Collection("WebSDRDriverRegistry")]
public class WebSdrRegistryTests
{
    private const string WebsdrOrgHtml = """
        <html><head>
        <script src="websdr-base.js?3"></script>
        </head><body>Twente wideband receiver</body></html>
        """;

    private const string KiwisdrHtml = """
        <html><head>
        <script src="kiwisdr.min.js?v=1.2"></script>
        </head><body>KiwiSDR</body></html>
        """;

    private const string OpenwebrxHtml = """
        <html><head>
        <script src="compiled/receiver.js"></script>
        </head><body>OpenWebRX</body></html>
        """;

    // UberSDR serves two interfaces: the root page is the older one, and
    // /v2/ is the one carrying the control API this driver speaks. Both
    // are recognised, because an operator publishes (and will paste) the
    // root -- the driver navigates to v2 itself. See UberSDRDriver.V2PageUrl().
    private const string UbersdrV2Html = """
        <html><head>
        <script src="vendor/react.production.min.js"></script>
        <script src="dist/v2.js"></script>
        </head><body><div id="root"></div></body></html>
        """;

    private const string UbersdrRootHtml = """
        <html><head>
        <script src="browser-extension-detector.js"></script>
        <script src="dist/app.bundle.min.js"></script>
        </head><body>UberSDR</body></html>
        """;

    private const string UnrelatedHtml = """
        <html><head>
        <script src="jquery.min.js"></script>
        </head><body>Some other site</body></html>
        """;

    private const string BothMarkersHtml = """
        <html><head>
        <script src="websdr-base.js?3"></script>
        <script src="kiwisdr.min.js"></script>
        </head><body>Ambiguous</body></html>
        """;

    private const string AllThreeMarkersHtml = """
        <html><head>
        <script src="websdr-base.js?3"></script>
        <script src="kiwisdr.min.js"></script>
        <script src="compiled/receiver.js"></script>
        </head><body>Ambiguous</body></html>
        """;

    private const string MarkerOnlyInBodyTextHtml = """
        <html><head>
        <script src="jquery.min.js"></script>
        </head><body>
        <!-- this station used to run kiwisdr.min.js before switching software -->
        <p>We used websdr-base.js in the past.</p>
        </body></html>
        """;

    [Fact]
    public void DetectsWebsdrOrg() => Assert.Equal("websdr_org", WebSdrRegistry.DetectDriverType(WebsdrOrgHtml));

    [Fact]
    public void DetectsKiwisdr() => Assert.Equal("kiwisdr", WebSdrRegistry.DetectDriverType(KiwisdrHtml));

    [Fact]
    public void DetectsOpenwebrx() => Assert.Equal("openwebrx", WebSdrRegistry.DetectDriverType(OpenwebrxHtml));

    [Fact]
    public void DetectsUbersdrV2Page() => Assert.Equal("ubersdr", WebSdrRegistry.DetectDriverType(UbersdrV2Html));

    [Fact]
    public void DetectsUbersdrRootPage() =>
        // The address an operator publishes. Recognising it is what makes
        // pasting the obvious URL work; the driver resolves it to /v2/ on
        // its own.
        Assert.Equal("ubersdr", WebSdrRegistry.DetectDriverType(UbersdrRootHtml));

    [Fact]
    public void UbersdrMarkers_DoNotMatchAnUnrelatedReactSite()
    {
        // Neither marker is a generic filename: "dist/v2.js" is UberSDR's
        // own bundle path and browser-extension-detector.js is its own
        // script. A page that merely ships React is not a match.
        const string reactOnly = """
            <html><head><script src="vendor/react.production.min.js"></script>
            <script src="dist/bundle.js"></script></head><body></body></html>
            """;
        Assert.Null(WebSdrRegistry.DetectDriverType(reactOnly));
    }

    [Fact]
    public void NoMarker_ReturnsNull() => Assert.Null(WebSdrRegistry.DetectDriverType(UnrelatedHtml));

    [Fact]
    public void AmbiguousMatches_ReturnNullNotFirstRegistered() => Assert.Null(WebSdrRegistry.DetectDriverType(BothMarkersHtml));

    [Fact]
    public void ThreeWayAmbiguousMatches_ReturnNull() => Assert.Null(WebSdrRegistry.DetectDriverType(AllThreeMarkersHtml));

    [Fact]
    public void MarkerStringInBodyText_IsNotAMatch() => Assert.Null(WebSdrRegistry.DetectDriverType(MarkerOnlyInBodyTextHtml));

    [Fact]
    public void RegisterBuiltinDrivers_PopulatesAllFourDriverTypes()
    {
        // Isolated from the rest of the suite via a fresh dictionary swap
        // would be ideal, but WebSDRDriverRegistry.Drivers is a shared
        // static (see EngineSwitchSiteTests' own doc comment on the same
        // point) -- registering the same four factories again here is
        // idempotent and harmless regardless of what ran before.
        WebSdrRegistry.RegisterBuiltinDrivers();
        foreach (var driverType in new[] { "websdr_org", "kiwisdr", "openwebrx", "ubersdr" })
        {
            Assert.True(WebSDRDriverRegistry.Drivers.ContainsKey(driverType));
            var driver = WebSDRDriverRegistry.Drivers[driverType]("http://example.invalid/", 0);
            Assert.NotNull(driver);
        }
    }
}
