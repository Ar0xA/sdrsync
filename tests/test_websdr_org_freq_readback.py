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


# ------------------------------------------------------------------
# Regression: the corrective re-tune above was DEAD CODE before the
# window.freq fix (the old #freqinput read always returned None, so
# _verify_freq_applied never got far enough to compare anything). Making
# the readback real for the first time also made that corrective re-tune
# live for the first time -- and it turned out to unconditionally force
# the page back to whatever WE last pushed on any mismatch, with no way
# to tell "our own setfreq() write silently didn't land" apart from "the
# user clicked a different frequency on the page in the meantime". Found
# live: a forward push arms a 0.6s (FREQ_VERIFY_DELAY_S) verify window,
# and a user's manual click landing inside that window got silently
# reverted back to the rig's frequency, logging a misleading "WebSDR did
# not apply requested frequency" warning that wasn't true -- the page HAD
# applied a frequency, just not ours.
#
# The fix: tune_hz() now captures the page's freq BEFORE calling
# setfreq() (pre_push_hz) and passes it through to the verify task. A
# mismatch against expected_hz is only "our write didn't land" (and
# therefore worth correcting) if the page is still sitting on its
# pre-push value; a mismatch against some THIRD value means something
# else moved it in the meantime, and the verify task now backs off
# instead of fighting whoever/whatever that was.

def test_user_click_during_verify_window_is_not_reverted():
    """The core regression, reproduced directly: the page moved to a
    value that is neither the requested frequency NOR its pre-push value
    -- exactly what a user's manual click looks like -- and the verify
    task must leave it alone."""
    driver = make_driver()
    driver._page.freq_khz = 14200.0  # the user's own click, already landed

    async def run():
        # expected_hz=14074000 with pre_push_hz=14000000: the page was
        # supposedly at 14000 kHz right before our push, but now shows
        # 14200 kHz -- neither value, so this must read as a user edit.
        await driver._verify_freq_applied(14_074_000, pre_push_hz=14_000_000)

    asyncio.run(run())
    assert driver._page.setband_calls == []
    assert driver._page.setfreq_calls == []
    assert driver._last_tune_error is None
    # The page's value (the user's click) must survive untouched.
    assert driver._page.freq_khz == 14200.0


def test_write_that_genuinely_did_not_land_is_still_corrected():
    """Contrast case: the page is STILL on its pre-push value (our own
    setfreq() call silently didn't take -- the original failure mode this
    corrective re-tune exists for) -- the retune must still fire exactly
    as before."""
    driver = make_driver()
    driver._page.freq_khz = 14074.0  # unchanged since before our (failed) push

    async def run():
        # expected_hz=14075000, and the page is still sitting on exactly
        # what it was pre-push (14074000) -- our write never landed.
        await driver._verify_freq_applied(14_075_000, pre_push_hz=14_074_000)

    asyncio.run(run())
    assert driver._page.setband_calls == [0]
    assert driver._page.setfreq_calls == [14075.0]


def test_unknown_pre_push_value_falls_back_to_correcting_on_mismatch():
    """When pre_push_hz isn't available (None -- the default, e.g. an
    older caller or a failed pre-push read), any mismatch is ambiguous,
    so this preserves the original, more conservative self-heal
    behaviour rather than silently going quiet on every real failure."""
    driver = make_driver()
    driver._page.freq_khz = 14200.0  # could be a stuck write OR a user click -- can't tell

    async def run():
        await driver._verify_freq_applied(14_074_000)  # pre_push_hz omitted -> None

    asyncio.run(run())
    assert driver._page.setband_calls == [0]
    assert driver._page.setfreq_calls == [14074.0]


def test_concurrent_close_during_the_pre_push_read_does_not_crash():
    """Regression test for a real bug found by a code review pass: the
    pre-push readback above introduced a new `await` between tune_hz()'s
    entry `_attached` check and its first page-writing call (there was
    none before -- every prior await was already downstream of that first
    write). If close() runs during that new window -- e.g. the user hits
    Disconnect, or SwitchWebsdr tears this driver down, while a forward
    push is in flight -- self._page becomes None, and the very next
    self._page.evaluate() call crashed with AttributeError instead of
    tune_hz() cleanly returning False like every other "not attached
    any more" path in this method already does."""
    driver = make_driver()
    reached_pre_push_read = asyncio.Event()
    resume_pre_push_read = asyncio.Event()
    real_evaluate = driver._page.evaluate

    async def suspending_evaluate(js, *args):
        # Only the pre-push readback (a bare window.freq check) --
        # setfreq/setband calls must not suspend, or this wouldn't be
        # exercising the specific window this test targets.
        if "window.freq" in js and "setfreq" not in js:
            reached_pre_push_read.set()
            await resume_pre_push_read.wait()
        return await real_evaluate(js, *args)

    driver._page.evaluate = suspending_evaluate

    async def do_tune():
        return await driver.tune_hz(14_074_000, verify=True)

    async def do_close_once_read_is_in_flight():
        await reached_pre_push_read.wait()
        await driver.close()
        resume_pre_push_read.set()

    async def run():
        return await asyncio.gather(do_tune(), do_close_once_read_is_in_flight())

    tune_result, _ = asyncio.run(run())
    assert tune_result is False
    assert driver._attached is False


def test_tune_hz_captures_and_forwards_the_pre_push_value():
    """End-to-end: tune_hz() itself must actually read and pass
    pre_push_hz through _schedule_freq_verification(), not just support
    it as a parameter nothing ever supplies."""
    driver = make_driver()
    driver._page.freq_khz = 14100.0  # the value in place before this tune

    captured = {}
    real_schedule = driver._schedule_freq_verification

    def spy(expected_hz, pre_push_hz=None):
        captured["expected_hz"] = expected_hz
        captured["pre_push_hz"] = pre_push_hz
        # Don't actually schedule the real background task -- this test
        # only cares what tune_hz() hands off, not the verify task itself.

    driver._schedule_freq_verification = spy

    asyncio.run(driver.tune_hz(14_074_000, verify=True))

    assert captured == {"expected_hz": 14_074_000, "pre_push_hz": 14_100_000}
