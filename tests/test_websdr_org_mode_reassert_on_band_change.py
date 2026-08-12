"""websdr-base.js's setband() silently flips window.mode USB<->LSB (and
swaps its hi/lo filter offsets) whenever a band crossing changes
islsbband() -- confirmed against the live source at
websdr.ewi.utwente.nl:8901. tune_hz() must re-assert the last-pushed mode
after every setband() call, or a band crossing can leave the page on the
wrong sideband with no error and nothing catching it (window.freq is
unaffected by the flip, so the freq-readback verification can't see it)."""
import asyncio

from sdrsync.websdr.websdr_org import WebsdrOrgDriver


class StubPage:
    def __init__(self) -> None:
        self.calls: list = []

    async def evaluate(self, js, *args):
        self.calls.append((js, args))
        return None


def make_driver() -> WebsdrOrgDriver:
    driver = WebsdrOrgDriver("http://example.invalid/")
    driver._attached = True
    # Two bands: 80m (LSB-default per islsbband) and 20m (not).
    driver._bands = [(3_500_000, 4_000_000), (14_000_000, 14_350_000)]
    driver._page = StubPage()
    return driver


def _js_names(calls):
    """window.setband(idx) / window.set_mode(m) / window.setfreq(khz) --
    identify each call by which global function it invokes."""
    names = []
    for js, args in calls:
        if "setband" in js:
            names.append(("setband", args[0]))
        elif "set_mode" in js:
            names.append(("set_mode", args[0]))
        elif "setfreq" in js:
            names.append(("setfreq", args[0]))
    return names


def test_band_crossing_reasserts_last_pushed_mode():
    driver = make_driver()
    asyncio.run(driver.set_mode("USB", 2400))  # pushes web_mode "USB", band still None -> no setband yet
    driver._page.calls.clear()

    ok = asyncio.run(driver.tune_hz(14_074_000, verify=False))  # crosses into band 1

    assert ok is True
    calls = _js_names(driver._page.calls)
    assert ("setband", 1) in calls
    assert ("set_mode", "USB") in calls
    # Order matters: mode must be restored strictly after setband() (which
    # is what clobbers it) and strictly before the final setfreq().
    setband_i = calls.index(("setband", 1))
    set_mode_i = calls.index(("set_mode", "USB"))
    setfreq_i = calls.index(("setfreq", 14074.0))
    assert setband_i < set_mode_i < setfreq_i


def test_no_band_change_does_not_reassert_mode():
    driver = make_driver()
    asyncio.run(driver.set_mode("USB", 2400))
    driver._current_band = 1  # already on the 20m band
    driver._page.calls.clear()

    ok = asyncio.run(driver.tune_hz(14_100_000, verify=False))  # stays in band 1

    assert ok is True
    calls = _js_names(driver._page.calls)
    assert ("setband", 1) not in calls
    assert not any(name == "set_mode" for name, _ in calls)


def test_no_reassert_when_mode_never_pushed():
    """Tuning before any set_mode() call has ever run must not call
    window.set_mode(None) or otherwise error -- self._current_web_mode is
    still None in that state."""
    driver = make_driver()
    driver._page.calls.clear()

    ok = asyncio.run(driver.tune_hz(14_074_000, verify=False))

    assert ok is True
    calls = _js_names(driver._page.calls)
    assert not any(name == "set_mode" for name, _ in calls)
