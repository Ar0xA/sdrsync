import asyncio
import logging

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



def _tune(driver, freq_hz):
    return asyncio.run(driver.tune_hz(freq_hz))


def test_out_of_range_warning_is_rate_limited_on_exact_repeats(caplog):
    """v14: the engine keeps retrying a rejected forward push on its own
    schedule, so a rig parked on a frequency this receiver simply doesn't
    cover would otherwise emit an identical WARNING forever. Same
    rate-limiting shape as _last_unmapped_mode / openwebrx.py's
    _last_out_of_range_key: WARNING when the condition changes, DEBUG on
    exact repeats."""
    d = make_driver([(7100.0, 200.0)])
    d._attached = True

    with caplog.at_level(logging.DEBUG, logger="sdrsync.websdr.websdr_org"):
        assert _tune(d, 14_074_000) is False
        first = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(first) == 1

        caplog.clear()
        assert _tune(d, 14_074_000) is False  # same frequency, still rejected
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
        assert any(r.levelno == logging.DEBUG for r in caplog.records)

        caplog.clear()
        assert _tune(d, 21_000_000) is False  # a DIFFERENT rejected frequency
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_out_of_range_message_is_still_reported_to_the_gui_on_repeats():
    """Rate-limiting the LOG must not rate-limit the status the GUI
    shows -- _last_tune_error is what surfaces the rejection on screen."""
    d = make_driver([(7100.0, 200.0)])
    d._attached = True

    _tune(d, 14_074_000)
    _tune(d, 14_074_000)
    assert d._last_tune_error is not None
    assert "outside all bands" in d._last_tune_error
