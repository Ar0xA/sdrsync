"""get_status()'s displayed mode must be normalized to its base (non-narrow)
form -- ext_get_mode() returns KiwiSDR's own internal string verbatim (e.g.
"usn" for a narrow-filter USB selection), which reads as a different mode
than the hamlib USB it corresponds to if shown raw. Regression coverage for
the v9.1 fix (reported as "mode shows USN/LSN instead of USB/LSB")."""
import asyncio

from sdrsync.websdr.kiwisdr import KiwiSDRDriver


class StubPage:
    def __init__(self, result: dict) -> None:
        self._result = result

    async def evaluate(self, _js, *_args):
        return self._result


def _get_status(mode: str):
    driver = KiwiSDRDriver("http://example.invalid")
    driver._attached = True
    driver._page = StubPage({"freq": "14074.00", "mode": mode, "audio": True})
    return asyncio.run(driver.get_status())


def test_narrow_usb_displays_as_usb():
    assert _get_status("usn").mode == "USB"


def test_narrow_lsb_displays_as_lsb():
    assert _get_status("lsn").mode == "LSB"


def test_wide_usb_still_displays_as_usb():
    assert _get_status("usb").mode == "USB"


def test_narrow_cw_displays_as_cw():
    assert _get_status("cwn").mode == "CW"
