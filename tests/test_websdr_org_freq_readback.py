"""Frequency read-back on websdr.org sites (v2.2.2 live-testing round).

Regression guard for a bug found by driving the REAL WebsdrOrgDriver
against the REAL Twente site (http://websdr.ewi.utwente.nl:8901/) in a
real wx WebView: the driver read back the tuned frequency from a
`#freqinput` element that DOES NOT EXIST on any websdr.org build. The
visible box is `document.freqform.frequency` (a `name`, no `id`) --
verified live against five instances (Twente, Hack Green, IS0GRB, NA5B,
Maasbree). So `_read_page_freq_hz()` always returned None,
`_verify_freq_applied()` was permanently dead, and every single tune
logged "Could not read back ... to verify setfreq()" (hundreds of such
lines in a real user's log).

The read must come from the page's own global `freq` (kHz), because the
visible box is unusable for this on two independent counts, both read
straight off the site's websdr-base.js:

  * it is written as `nomfreq.toFixed(2)` -- rounded to 10 Hz -- so it
    cannot confirm a sub-10 Hz tune at all, even though the site itself
    sends full 1 Hz resolution upstream as "f=" + freq.toFixed(3); and
  * in CW it shows `nominalfreq()` == carrier + (hi+lo)/2, whereas
    tune_hz() verifies against the carrier/BFO frequency -- so comparing
    against the box would be off by the CW filter centre (-750 Hz with
    the site's default CW filter, confirmed live) and would trip the
    mismatch/band-reselect retry on EVERY CW tune.

StubWebsdrPage below therefore models the real page faithfully: no
`#freqinput`, a `freq` global stored by setfreq(), and a display string
that is rounded AND CW-shifted exactly like the real one.
"""
import asyncio
import logging

import pytest

from sdrsync.websdr.websdr_org import FREQ_VERIFY_TOLERANCE_HZ, WebsdrOrgDriver

# websdr-base.js set_mode("CW") -> setmf("cw", -0.95, -0.55); the display
# shows nominalfreq() = freq + (hi+lo)/2 = freq - 0.75 kHz.
CW_LO_KHZ, CW_HI_KHZ = -0.95, -0.55
SSB_LO_KHZ, SSB_HI_KHZ = 0.3, 2.7


class StubWebsdrPage:
    """Faithful stand-in for a real websdr.org page's JS surface."""

    def __init__(self) -> None:
        self.freq_khz = None          # the page's own `freq` global, in kHz
        self.lo, self.hi = SSB_LO_KHZ, SSB_HI_KHZ
        self.band = 0
        self.setfreq_calls: list[float] = []
        self.setband_calls: list[int] = []

    # -- what the real page would show in document.freqform.frequency --
    def _display(self) -> str:
        if self.freq_khz is None:
            return ""
        nominal = self.freq_khz
        if self.lo < 0 and self.hi < 0:      # iscw()
            nominal = self.freq_khz + (self.hi + self.lo) / 2
        return f"{nominal:.2f}"              # <- the real 10 Hz rounding

    async def evaluate(self, js, *args):
        if "window.setfreq" in js:
            self.freq_khz = float(args[0])
            self.setfreq_calls.append(self.freq_khz)
            return None
        if "window.setband" in js:
            self.band = args[0]
            self.setband_calls.append(args[0])
            return None
        if "window.set_mode" in js:
            if str(args[0]).upper().startswith("CW"):
                self.lo, self.hi = CW_LO_KHZ, CW_HI_KHZ
            else:
                self.lo, self.hi = SSB_LO_KHZ, SSB_HI_KHZ
            return None
        if "window.freq" in js:
            return self.freq_khz
        if "freqinput" in js:
            # The real page has no such element -- this is the whole point.
            return None
        if "freqform" in js:
            return self._display()
        return None


def make_driver(cw_offset_hz: int = 0) -> WebsdrOrgDriver:
    driver = WebsdrOrgDriver("http://example.invalid/", cw_offset_hz=cw_offset_hz)
    driver._attached = True
    driver._bands = [(0, 30_000_000)]
    driver._current_band = 0
    driver._page = StubWebsdrPage()
    return driver


async def _tune_then_read(driver: WebsdrOrgDriver, freq_hz: int) -> int:
    await driver.tune_hz(freq_hz, verify=False)
    return await driver._read_page_freq_hz()


@pytest.mark.parametrize("freq_hz", [14_074_500, 14_074_499, 14_074_498, 14_075_000])
def test_readback_is_exact_to_one_hz(freq_hz):
    """The core regression: reading back a tuned frequency must return it
    exactly, including 1 Hz steps the visible box rounds away. Against the
    old `#freqinput` read this returns None and fails."""
    driver = make_driver()
    assert asyncio.run(_tune_then_read(driver, freq_hz)) == freq_hz


def test_one_hz_steps_are_distinguishable_on_readback():
    """Two frequencies 1 Hz apart must read back as two different values --
    the site's own 2-decimal display collapses them into one string."""
    driver = make_driver()

    async def run():
        a = await _tune_then_read(driver, 14_074_500)
        b = await _tune_then_read(driver, 14_074_499)
        return a, b, driver._page._display()

    a, b, display = asyncio.run(run())
    assert (a, b) == (14_074_500, 14_074_499)
    assert a - b == 1
    # ...and this is exactly why the display can't be used for the check.
    assert display == "14074.50"


def test_verification_confirms_without_warning_or_corrective_retune(caplog):
    """A successful tune must verify cleanly: no "could not read back"
    warning, and no corrective setband/setfreq re-tune. Against the old
    `#freqinput` read the readback is None, which logs that warning."""
    driver = make_driver()

    async def run():
        await driver.tune_hz(14_074_499, verify=False)
        driver._page.setband_calls.clear()
        driver._page.setfreq_calls.clear()
        with caplog.at_level(logging.WARNING, logger="sdrsync.websdr.websdr_org"):
            await driver._verify_freq_applied(14_074_499)

    asyncio.run(run())
    assert caplog.text == "" or "read back" not in caplog.text
    assert driver._page.setband_calls == []
    assert driver._page.setfreq_calls == []
    assert driver._last_tune_error is None


def test_cw_readback_is_the_carrier_not_the_shifted_display():
    """In CW the page DISPLAYS carrier + (hi+lo)/2 but stores the carrier
    in `freq`. Verification compares against the carrier, so reading the
    display would be off by the CW filter centre (750 Hz here -- far over
    FREQ_VERIFY_TOLERANCE_HZ) and would trip a bogus corrective re-tune on
    every CW tune. Guards against "fixing" this by parsing the box."""
    driver = make_driver()

    async def run():
        await driver.set_mode("CW", 500)
        await driver.tune_hz(14_060_000, verify=False)
        readback = await driver._read_page_freq_hz()
        display = driver._page._display()
        driver._page.setband_calls.clear()
        driver._page.setfreq_calls.clear()
        await driver._verify_freq_applied(14_060_000)
        return readback, display

    readback, display = asyncio.run(run())
    assert readback == 14_060_000              # the carrier, exactly
    assert display == "14059.25"               # what the user actually sees
    assert abs(int(round(float(display) * 1000)) - 14_060_000) > FREQ_VERIFY_TOLERANCE_HZ
    # No corrective re-tune was triggered by the display/carrier gap.
    assert driver._page.setband_calls == []
    assert driver._page.setfreq_calls == []


def test_readback_returns_none_when_page_has_no_freq_global():
    """A page that isn't a websdr.org page at all still degrades to None
    rather than raising."""
    driver = make_driver()
    assert asyncio.run(driver._read_page_freq_hz()) is None
