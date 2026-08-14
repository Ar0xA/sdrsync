"""websdr-base.js's setband() silently flips window.mode USB<->LSB (and
swaps its hi/lo filter offsets) whenever a band crossing changes
islsbband() -- confirmed against the live source at
websdr.ewi.utwente.nl:8901. tune_hz() must re-assert the last-pushed mode
(+ exact filter, if one was pushed via window.setmf()) after every
setband() call, or a band crossing can leave the page on the wrong
sideband -- or the wrong filter width -- with no error and nothing
catching it (window.freq is unaffected by the flip, so the freq-readback
verification can't see it)."""
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
    """window.setband(idx) / window.setmf(m, lo, hi) / window.set_mode(m) /
    window.setfreq(khz) -- identify each call by which global function it
    invokes. setmf's single dict arg is flattened into a 3-tuple so call
    sites can compare it the same way as the others."""
    names = []
    for js, args in calls:
        if "setband" in js:
            names.append(("setband", args[0]))
        elif "setmf" in js:
            a = args[0]
            names.append(("setmf", a["m"], a["lo"], a["hi"]))
        elif "set_mode" in js:
            names.append(("set_mode", args[0]))
        elif "setfreq" in js:
            names.append(("setfreq", args[0]))
    return names


def test_band_crossing_reasserts_last_pushed_mode_and_filter():
    driver = make_driver()
    asyncio.run(driver.set_mode("USB", 2400))  # pushes setmf("USB", 0.3, 2.7); band still None -> no setband yet
    driver._page.calls.clear()

    ok = asyncio.run(driver.tune_hz(14_074_000, verify=False))  # crosses into band 1

    assert ok is True
    calls = _js_names(driver._page.calls)
    assert ("setband", 1) in calls
    assert ("setmf", "USB", 0.3, 2.7) in calls
    # Order matters: mode/filter must be restored strictly after setband()
    # (which is what clobbers it) and strictly before the final setfreq().
    setband_i = calls.index(("setband", 1))
    setmf_i = calls.index(("setmf", "USB", 0.3, 2.7))
    setfreq_i = calls.index(("setfreq", 14074.0))
    assert setband_i < setmf_i < setfreq_i


def test_band_crossing_reasserts_the_sites_own_preset_when_no_passband_known():
    """set_mode() with no usable passband_hz falls back to the site's own
    set_mode() preset (see passband_edges()'s None case) -- band-crossing
    reassert must replay THAT same fallback, not invent a filter."""
    driver = make_driver()
    asyncio.run(driver.set_mode("USB", None))  # no passband_hz -> window.set_mode("USB"), no setmf
    driver._page.calls.clear()

    ok = asyncio.run(driver.tune_hz(14_074_000, verify=False))

    assert ok is True
    calls = _js_names(driver._page.calls)
    assert ("setband", 1) in calls
    assert ("set_mode", "USB") in calls
    assert not any(name == "setmf" for name, *_ in calls)


def test_no_band_change_does_not_reassert_mode():
    driver = make_driver()
    asyncio.run(driver.set_mode("USB", 2400))
    driver._current_band = 1  # already on the 20m band
    driver._page.calls.clear()

    ok = asyncio.run(driver.tune_hz(14_100_000, verify=False))  # stays in band 1

    assert ok is True
    calls = _js_names(driver._page.calls)
    assert ("setband", 1) not in calls
    assert not any(name in ("setmf", "set_mode") for name, *_ in calls)


def test_no_reassert_when_mode_never_pushed():
    """Tuning before any set_mode() call has ever run must not call
    window.setmf()/window.set_mode() or otherwise error --
    self._current_mode_call is still None in that state."""
    driver = make_driver()
    driver._page.calls.clear()

    ok = asyncio.run(driver.tune_hz(14_074_000, verify=False))

    assert ok is True
    calls = _js_names(driver._page.calls)
    assert not any(name in ("setmf", "set_mode") for name, *_ in calls)
