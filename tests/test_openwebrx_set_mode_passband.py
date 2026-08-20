"""OpenWebRXDriver.set_mode() drives an EXACT filter width via a
follow-up demodulator.setBandpass() call -- see passband_edges() in
openwebrx.py. Confirmed against the live site's real JS that
DemodulatorPanel.setMode() only reconstructs the demodulator (which is
what actually applies a mode's default bandpass) when the mode is
genuinely CHANGING; for an unchanged mode, demod.low_cut/high_cut is
just whatever was previously applied, NOT that mode's default. So
set_mode() reads the TRUE per-mode default via
Modes.findByModulation(mode).bandpass (a separate, stable lookup, not
demod state) and always issues a follow-up setBandpass() call -- either
the rig-driven width or that true default -- so an unchanged mode still
deterministically resets the filter instead of leaving a stale custom
width in place.

StubPage below models this distinction explicitly: demod_mode/low_cut/
high_cut are CURRENT demodulator state (only reset by setMode() when
the mode actually changes, mirroring the real JS's early-return); modes
is the STATIC per-mode default table (Modes.findByModulation), which
never changes regardless of demodulator state. The setBandpass branch
applies its args unconditionally -- there's no JS engine here to
execute the real guard clause (a mode-change race between the two round
trips) -- so that guard is instead checked directly against the JS
source text sent for the call (see
test_second_call_guards_against_a_mode_change_racing_it)."""
import asyncio

from sdrsync.websdr.openwebrx import OpenWebRXDriver


class StubPage:
    def __init__(self, modes: dict) -> None:
        self.modes = modes  # mode -> (default_low, default_high), the static per-mode config
        self.calls: list = []
        self.demod_mode = None
        self.low_cut = None
        self.high_cut = None

    async def evaluate(self, js, *args):
        self.calls.append((js, args))
        if "setMode" in js:
            mode = args[0]
            if mode != self.demod_mode:
                # Mirrors the real page: setMode() only reconstructs the
                # demodulator (applying the new mode's own default
                # bandpass) when the mode is actually changing.
                self.demod_mode = mode
                self.low_cut, self.high_cut = self.modes.get(mode, (None, None))
            default_low, default_high = self.modes.get(mode, (None, None))
            return {
                "status": "ok", "mode": self.demod_mode,
                "default_low": default_low, "default_high": default_high,
            }
        if "setBandpass" in js:
            # The guard clause (mode-match check) lives INSIDE the JS
            # text itself -- this stub has no JS engine to execute it,
            # so it can't faithfully simulate that conditional. Applies
            # unconditionally; test_second_call_guards_against_a_mode_
            # change_racing_it() below checks the guard is present in
            # the JS source directly instead.
            a = args[0]
            self.low_cut, self.high_cut = a["lo"], a["hi"]
            return "applied"
        return None


def make_driver(modes: dict) -> OpenWebRXDriver:
    driver = OpenWebRXDriver("http://example.invalid/")
    driver._attached = True
    driver._page = StubPage(modes)
    return driver


def _setbandpass_calls(driver: OpenWebRXDriver):
    return [args[0] for js, args in driver._page.calls if "setBandpass" in js]


def test_set_mode_with_passband_overrides_the_default_with_exact_edges():
    driver = make_driver({"usb": (300, 2700)})
    ok = asyncio.run(driver.set_mode("USB", 1200))

    assert ok is True
    calls = _setbandpass_calls(driver)
    assert calls == [{"mode": "usb", "lo": 300, "hi": 1500}]  # passband_edges("usb", 1200, (300, 2700))
    assert (driver._page.low_cut, driver._page.high_cut) == (300, 1500)


def test_set_mode_without_passband_explicitly_reapplies_the_true_default():
    driver = make_driver({"usb": (300, 2700)})
    ok = asyncio.run(driver.set_mode("USB", None))

    assert ok is True
    assert _setbandpass_calls(driver) == [{"mode": "usb", "lo": 300, "hi": 2700}]
    assert (driver._page.low_cut, driver._page.high_cut) == (300, 2700)


def test_turning_off_passband_sync_mid_session_actually_restores_the_default():
    """Bug-hunter finding: the mode stays USB throughout (the real page
    never reconstructs the demodulator for an unchanged mode), only
    passband_hz changes from a rig-derived width to None -- as
    AppSettings.sync_passband_from_rig being turned off would produce.
    Must not leave the earlier custom width in place."""
    driver = make_driver({"usb": (300, 2700)})
    asyncio.run(driver.set_mode("USB", 1800))
    assert (driver._page.low_cut, driver._page.high_cut) == (300, 2100)

    ok = asyncio.run(driver.set_mode("USB", None))

    assert ok is True
    assert (driver._page.low_cut, driver._page.high_cut) == (300, 2700)  # true default, not left at (300, 2100)


def test_set_mode_cw_uses_whatever_centre_the_true_default_reports():
    """No hardcoded CW centre in this driver (unlike websdr_org.py/
    kiwisdr.py) -- confirms the true per-mode default drives the centre,
    here (300, 700) i.e. +500 Hz."""
    driver = make_driver({"cw": (300, 700)})
    ok = asyncio.run(driver.set_mode("CW", 200))

    assert ok is True
    assert _setbandpass_calls(driver) == [{"mode": "cw", "lo": 400, "hi": 600}]


def test_mode_mismatch_after_setmode_is_still_reported_as_failure():
    """A mode that doesn't apply must fail set_mode() the same as before
    -- and must not attempt a setBandpass() call for a mode that never
    actually took."""
    driver = make_driver({"usb": (300, 2700)})

    class StuckPage(StubPage):
        async def evaluate(self, js, *args):
            if "setMode" in js:
                return {"status": "ok", "mode": "am", "default_low": -4500, "default_high": 4500}
            return await super().evaluate(js, *args)

    driver._page = StuckPage({"usb": (300, 2700)})
    ok = asyncio.run(driver.set_mode("USB", 2400))

    assert ok is False
    assert _setbandpass_calls(driver) == []
    assert driver._last_mode_error is not None


def test_second_call_guards_against_a_mode_change_racing_it():
    """Bug-hunter finding: between the two round trips (read mode+default,
    then apply filter), the operator could click a different mode on the
    page -- the filter computed for the FIRST mode must never land on
    whatever demodulator exists by the time the second call runs. A
    Python-side stub can't execute the embedded JS conditional itself
    (no JS engine here), so what's actually checkable from this side is
    that the real guard clause is present in the JS text sent for the
    setBandpass call -- same pattern as this repo's other JS-text
    assertions (e.g. test_kiwisdr_audio_unlock_click.py's "querySelector"
    check)."""
    driver = make_driver({"usb": (300, 2700)})
    ok = asyncio.run(driver.set_mode("USB", 1200))

    assert ok is True
    setbandpass_call = next(js for js, args in driver._page.calls if "setBandpass" in js)
    assert "d.modulation !== a.mode" in setbandpass_call


def test_mode_change_race_is_reported_as_failure_not_success():
    driver = make_driver({"usb": (300, 2700)})

    class RacingPage(StubPage):
        async def evaluate(self, js, *args):
            if "setBandpass" in js:
                self.calls.append((js, args))
                return "mode_changed"
            return await super().evaluate(js, *args)

    driver._page = RacingPage({"usb": (300, 2700)})

    assert asyncio.run(driver.set_mode("USB", 1200)) is False
    assert "not applied" in driver._last_mode_error
