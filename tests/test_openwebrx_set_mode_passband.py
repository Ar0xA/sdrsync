"""OpenWebRXDriver.set_mode() drives an EXACT filter width via a
follow-up demodulator.setBandpass() call (confirmed against the live
site's real JS: setMode() always applies the new mode's own default
bandpass as part of constructing a fresh Demodulator, so set_mode()
reads that just-applied default back in the same round trip and issues
one more setBandpass() call to override it with an exact width from the
rig's passband_hz) -- see passband_edges() in openwebrx.py. Regression
coverage for that call shape, using a stub page that models
window.ws/demodulatorPanel/setMode/setBandpass the same way the other
OpenWebRX test files model the page surface."""
import asyncio

from sdrsync.websdr.openwebrx import OpenWebRXDriver


class StubPage:
    """default_bandpass_by_mode models each mode's own site-configured
    default (what a real setMode() call would apply automatically) --
    the equivalent of websdr_org.py's/kiwisdr.py's static fallback
    tables, but per-instance/server-sent for real OpenWebRX (see module
    docstring), so the stub supplies it explicitly instead."""

    def __init__(self, default_bandpass_by_mode: dict) -> None:
        self.default_bandpass_by_mode = default_bandpass_by_mode
        self.calls: list = []
        self.mode = None
        self.low_cut = None
        self.high_cut = None

    async def evaluate(self, js, *args):
        self.calls.append((js, args))
        if "setMode" in js:
            mode = args[0]
            self.mode = mode
            self.low_cut, self.high_cut = self.default_bandpass_by_mode.get(mode, (None, None))
            return {"status": "ok", "mode": self.mode, "low_cut": self.low_cut, "high_cut": self.high_cut}
        if "setBandpass" in js:
            a = args[0]
            self.low_cut, self.high_cut = a["lo"], a["hi"]
            return None
        return None


def make_driver(default_bandpass_by_mode: dict) -> OpenWebRXDriver:
    driver = OpenWebRXDriver("http://example.invalid/")
    driver._attached = True
    driver._page = StubPage(default_bandpass_by_mode)
    return driver


def _setbandpass_calls(driver: OpenWebRXDriver):
    return [args[0] for js, args in driver._page.calls if "setBandpass" in js]


def test_set_mode_with_passband_overrides_the_default_with_exact_edges():
    driver = make_driver({"usb": (300, 2700)})
    ok = asyncio.run(driver.set_mode("USB", 1200))

    assert ok is True
    calls = _setbandpass_calls(driver)
    assert calls == [{"lo": 300, "hi": 1500}]  # passband_edges("usb", 1200, (300, 2700))
    assert (driver._page.low_cut, driver._page.high_cut) == (300, 1500)


def test_set_mode_without_passband_leaves_the_just_applied_default_alone():
    driver = make_driver({"usb": (300, 2700)})
    ok = asyncio.run(driver.set_mode("USB", None))

    assert ok is True
    assert _setbandpass_calls(driver) == []  # no override call at all
    assert (driver._page.low_cut, driver._page.high_cut) == (300, 2700)  # setMode()'s own default, untouched


def test_set_mode_cw_uses_whatever_centre_the_live_default_reports():
    """No hardcoded CW centre in this driver (unlike websdr_org.py/
    kiwisdr.py) -- confirms the live default read back from setMode()
    is genuinely what drives the centre, here (300, 700) i.e. +500 Hz."""
    driver = make_driver({"cw": (300, 700)})
    ok = asyncio.run(driver.set_mode("CW", 200))

    assert ok is True
    assert _setbandpass_calls(driver) == [{"lo": 400, "hi": 600}]


def test_mode_mismatch_after_setmode_is_still_reported_as_failure():
    """A mode that doesn't apply must fail set_mode() the same as before
    -- and must not attempt a setBandpass() call for a mode that never
    actually took."""
    driver = make_driver({"usb": (300, 2700)})
    driver._page.mode = "wrong-mode-that-never-updates"

    class StuckPage(StubPage):
        async def evaluate(self, js, *args):
            if "setMode" in js:
                return {"status": "ok", "mode": "am", "low_cut": -4500, "high_cut": 4500}
            return await super().evaluate(js, *args)

    driver._page = StuckPage({"usb": (300, 2700)})
    ok = asyncio.run(driver.set_mode("USB", 2400))

    assert ok is False
    assert _setbandpass_calls(driver) == []
    assert driver._last_mode_error is not None
