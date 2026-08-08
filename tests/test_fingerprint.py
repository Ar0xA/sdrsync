from sdrsync.websdr.registry import detect_driver_type

WEBSDR_ORG_HTML = """
<html><head>
<script src="websdr-base.js?3"></script>
</head><body>Twente wideband receiver</body></html>
"""

KIWISDR_HTML = """
<html><head>
<script src="kiwisdr.min.js?v=1.2"></script>
</head><body>KiwiSDR</body></html>
"""

OPENWEBRX_HTML = """
<html><head>
<script src="compiled/receiver.js"></script>
</head><body>OpenWebRX</body></html>
"""

# UberSDR serves two interfaces: the root page is the older one, and /v2/ is the
# one carrying the control API this driver speaks. Both are recognised, because an
# operator publishes (and will paste) the root -- the driver navigates to v2
# itself. See v2_page_url().
UBERSDR_V2_HTML = """
<html><head>
<script src="vendor/react.production.min.js"></script>
<script src="dist/v2.js"></script>
</head><body><div id="root"></div></body></html>
"""

UBERSDR_ROOT_HTML = """
<html><head>
<script src="browser-extension-detector.js"></script>
<script src="dist/app.bundle.min.js"></script>
</head><body>UberSDR</body></html>
"""

UNRELATED_HTML = """
<html><head>
<script src="jquery.min.js"></script>
</head><body>Some other site</body></html>
"""

BOTH_MARKERS_HTML = """
<html><head>
<script src="websdr-base.js?3"></script>
<script src="kiwisdr.min.js"></script>
</head><body>Ambiguous</body></html>
"""

ALL_THREE_MARKERS_HTML = """
<html><head>
<script src="websdr-base.js?3"></script>
<script src="kiwisdr.min.js"></script>
<script src="compiled/receiver.js"></script>
</head><body>Ambiguous</body></html>
"""

MARKER_ONLY_IN_BODY_TEXT_HTML = """
<html><head>
<script src="jquery.min.js"></script>
</head><body>
<!-- this station used to run kiwisdr.min.js before switching software -->
<p>We used websdr-base.js in the past.</p>
</body></html>
"""


def test_detects_websdr_org():
    assert detect_driver_type(WEBSDR_ORG_HTML) == "websdr_org"


def test_detects_kiwisdr():
    assert detect_driver_type(KIWISDR_HTML) == "kiwisdr"


def test_detects_openwebrx():
    assert detect_driver_type(OPENWEBRX_HTML) == "openwebrx"


def test_detects_ubersdr_v2_page():
    assert detect_driver_type(UBERSDR_V2_HTML) == "ubersdr"


def test_detects_ubersdr_root_page():
    # The address an operator publishes. Recognising it is what makes pasting the
    # obvious URL work; the driver resolves it to /v2/ on its own.
    assert detect_driver_type(UBERSDR_ROOT_HTML) == "ubersdr"


def test_ubersdr_markers_do_not_match_an_unrelated_react_site():
    # Neither marker is a generic filename: "dist/v2.js" is UberSDR's own bundle
    # path and browser-extension-detector.js is its own script. A page that merely
    # ships React is not a match.
    react_only = """
    <html><head><script src="vendor/react.production.min.js"></script>
    <script src="dist/bundle.js"></script></head><body></body></html>
    """
    assert detect_driver_type(react_only) is None


def test_no_marker_returns_none():
    assert detect_driver_type(UNRELATED_HTML) is None


def test_ambiguous_matches_return_none_not_first_registered():
    assert detect_driver_type(BOTH_MARKERS_HTML) is None


def test_three_way_ambiguous_matches_return_none():
    assert detect_driver_type(ALL_THREE_MARKERS_HTML) is None


def test_marker_string_in_body_text_is_not_a_match():
    assert detect_driver_type(MARKER_ONLY_IN_BODY_TEXT_HTML) is None
