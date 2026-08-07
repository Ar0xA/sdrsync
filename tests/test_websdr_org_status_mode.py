"""get_status()'s displayed mode must be normalized to its base (non-narrow)
form -- window.mode can legitimately be e.g. "USBN" for a narrow-filter USB
selection, which reads as a different mode than the hamlib USB it
corresponds to if shown raw (mirrors set_mode()'s own self._current_mode
stripping). Regression coverage for the v9.1 fix."""
import asyncio

from sdrsync.websdr.websdr_org import WebsdrOrgDriver


class StubPage:
    def __init__(self, result: dict) -> None:
        self._result = result

    async def evaluate(self, _js, *_args):
        return self._result


def _get_status(mode):
    driver = WebsdrOrgDriver("http://example.invalid")
    driver._attached = True
    driver._page = StubPage({"freq": 14074.0, "mode": mode, "audio": True})
    return asyncio.run(driver.get_status())


def test_narrow_usb_displays_as_usb():
    assert _get_status("USBN").mode == "USB"


def test_narrow_lsb_displays_as_lsb():
    assert _get_status("LSBN").mode == "LSB"


def test_wide_usb_still_displays_as_usb():
    assert _get_status("USB").mode == "USB"


def test_narrow_am_displays_as_am():
    assert _get_status("AMN").mode == "AM"


def test_amsync_is_not_mistaken_for_a_narrow_mode():
    assert _get_status("AMSYNC").mode == "AMSYNC"
