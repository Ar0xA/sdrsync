"""Pure-function tests for each driver's _reverse_effective_hz() -- the
reverse-direction (WebSDR -> rig) un-application of cw_offset_hz,
symmetric to each driver's own tune_hz() forward application. No page/
attach needed since cw_offset_hz is set at construction and this method
never touches self._page."""
from sdrsync.websdr.kiwisdr import KiwiSDRDriver
from sdrsync.websdr.openwebrx import OpenWebRXDriver
from sdrsync.websdr.websdr_org import WebsdrOrgDriver

DRIVER_CLASSES = [WebsdrOrgDriver, KiwiSDRDriver, OpenWebRXDriver]


def test_cw_mode_subtracts_offset():
    for cls in DRIVER_CLASSES:
        driver = cls("http://example.invalid/", cw_offset_hz=700)
        assert driver._reverse_effective_hz(14070700, "CW") == 14070000


def test_non_cw_mode_leaves_freq_unchanged():
    for cls in DRIVER_CLASSES:
        driver = cls("http://example.invalid/", cw_offset_hz=700)
        assert driver._reverse_effective_hz(14070000, "USB") == 14070000


def test_none_freq_stays_none():
    for cls in DRIVER_CLASSES:
        driver = cls("http://example.invalid/", cw_offset_hz=700)
        assert driver._reverse_effective_hz(None, "CW") is None


def test_zero_offset_is_a_no_op_even_in_cw():
    for cls in DRIVER_CLASSES:
        driver = cls("http://example.invalid/", cw_offset_hz=0)
        assert driver._reverse_effective_hz(14070000, "CW") == 14070000
