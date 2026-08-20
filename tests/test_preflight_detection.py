from sdrsync import preflight


def test_detect_reports_ambiguous_fingerprints_distinctly():
    html = '<script src="websdr-base.js"></script><script src="kiwisdr.min.js"></script>'
    driver_type, message = preflight._identify_websdr_html("http://example.invalid/", html)

    assert driver_type is None
    assert "Ambiguous" in message
    assert "multiple drivers" in message


def test_detect_reports_unknown_fingerprint_without_ambiguity():
    driver_type, message = preflight._identify_websdr_html(
        "http://example.invalid/", "<html></html>"
    )

    assert driver_type is None
    assert "Could not identify" in message
    assert "Ambiguous" not in message
