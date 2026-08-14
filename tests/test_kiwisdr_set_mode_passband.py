"""KiwiSDRDriver.set_mode() drives an EXACT filter width via
ext_tune()'s own low_cut/high_cut params (confirmed against the live
site's real JS: ext_tune() only calls window.ext_set_passband() when
BOTH are given) instead of picking between a coarse narrow/wide mode
string -- see passband_edges() in kiwisdr.py. Regression coverage for
that call shape, using a stub page that models ws_snd/ext_tune/
ext_get_mode the same way the other KiwiSDR test files do."""
import asyncio

from sdrsync.websdr.kiwisdr import KiwiSDRDriver


class StubPage:
    def __init__(self) -> None:
        self.calls: list = []
        self.mode = "usb"

    async def evaluate(self, js, *args):
        self.calls.append((js, args))
        if "ext_tune" in js:
            a = args[0]
            self.mode = a["mode"]
            return "ok"
        if "ext_get_mode" in js:
            return self.mode
        return None


def make_driver() -> KiwiSDRDriver:
    driver = KiwiSDRDriver("http://example.invalid/")
    driver._attached = True
    driver._page = StubPage()
    return driver


def _ext_tune_args(driver: KiwiSDRDriver) -> dict:
    for js, args in driver._page.calls:
        if "ext_tune" in js:
            return args[0]
    raise AssertionError("ext_tune was never called")


def test_set_mode_with_passband_sends_exact_edges():
    driver = make_driver()
    ok = asyncio.run(driver.set_mode("USB", 2400))

    assert ok is True
    a = _ext_tune_args(driver)
    assert a["mode"] == "usb"
    assert (a["lo"], a["hi"]) == (300, 2700)  # passband_edges("usb", 2400)


def test_set_mode_without_passband_omits_the_filter():
    """No usable passband_hz -> lo/hi both None (serializes to JS null),
    which ext_tune()'s own isArg() check treats as "don't touch the
    filter" -- falls back to the site's own default for the mode."""
    driver = make_driver()
    ok = asyncio.run(driver.set_mode("USB", None))

    assert ok is True
    a = _ext_tune_args(driver)
    assert a["mode"] == "usb"
    assert (a["lo"], a["hi"]) == (None, None)


def test_set_mode_cw_uses_the_sites_own_500hz_centred_filter():
    driver = make_driver()
    driver._page.mode = "cw"
    ok = asyncio.run(driver.set_mode("CW", 400))

    assert ok is True
    a = _ext_tune_args(driver)
    assert a["mode"] == "cw"
    assert (a["lo"], a["hi"]) == (300, 700)  # passband_edges("cw", 400)


def test_readback_verification_only_checks_mode_not_filter():
    """get_mode() has no filter component -- a mode that applies
    correctly must not fail verification just because the stub (or a
    real page) can't distinguish filter widths at that layer."""
    driver = make_driver()
    ok = asyncio.run(driver.set_mode("LSB", 1200))
    assert ok is True
    assert driver._current_mode == "lsb"
