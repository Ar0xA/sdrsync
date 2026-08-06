from sdrsync.websdr.websdr_org import WebsdrOrgDriver


def make_driver(bands):
    """bands: list of (center_khz, samplerate_khz) like the real bandinfo.js entries."""
    d = WebsdrOrgDriver(url="http://example.invalid/")
    d._bands = [
        (c * 1000 - s * 1000 / 2, c * 1000 + s * 1000 / 2)
        for c, s in bands
    ]
    return d


def test_single_wideband_like_twente_covers_full_range():
    # Twente: centerfreq=14579.8 kHz, samplerate=29159.6 kHz -> ~0 to 29.16 MHz
    d = make_driver([(14579.8, 29159.6)])
    assert d._band_for_freq(14_074_000) == 0
    assert d._band_for_freq(100_000) == 0
    assert d._band_for_freq(29_000_000) == 0


def test_frequency_outside_all_bands_returns_none():
    d = make_driver([(14579.8, 29159.6)])
    assert d._band_for_freq(50_000_000) is None


def test_multi_band_site_picks_correct_band():
    # e.g. a 40m band centered at 7100 kHz (200 kHz wide) and a 20m band at 14200 (200 kHz wide)
    d = make_driver([(7100.0, 200.0), (14200.0, 200.0)])
    assert d._band_for_freq(7_150_000) == 0
    assert d._band_for_freq(14_250_000) == 1
    assert d._band_for_freq(10_000_000) is None
