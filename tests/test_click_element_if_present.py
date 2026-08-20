"""click_element_if_present() (sdrsync/websdr/browser_shim.py) -- shared
best-effort helper added for KiwiSDR/OpenWebRX's own audio-unlock overlays
(#id-play-button-container / #openwebrx-autoplay-overlay), which tie their
AudioContext.resume() specifically to a click on ONE on-page element, not
just any trusted click anywhere. Regression coverage for the real bug this
was built to fix: on WebKitGTK (native Linux desktop, non-WSL2), those
overlays actually render and were never clicked, leaving audio silent
forever even though tuning/mode control worked fine."""
import asyncio

from sdrsync.websdr import browser_shim
from sdrsync.websdr.browser_shim import BrowserError, click_element_if_present


class StubPage:
    def __init__(self, rect=None, raise_on_evaluate: bool = False, click_result=None):
        self._rect = rect
        self._raise_on_evaluate = raise_on_evaluate
        self._click_result = click_result
        self.evaluate_calls = 0
        self.click_calls: list = []
        self.mouse = self

    async def evaluate(self, js, *args):
        self.evaluate_calls += 1
        if self._raise_on_evaluate:
            raise BrowserError("simulated page error")
        return self._rect

    async def click(self, x, y):
        self.click_calls.append((x, y))
        return self._click_result


def _fast_polling(monkeypatch):
    monkeypatch.setattr(browser_shim, "CLICK_ELEMENT_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(browser_shim, "CLICK_ELEMENT_POLL_INTERVAL_S", 0.01)


def test_clicks_element_center_when_present():
    page = StubPage(rect={"x": 100.4, "y": 200.6})
    result = asyncio.run(click_element_if_present(page, "#some-overlay"))
    assert result is True
    assert page.click_calls == [(100, 201)]


def test_returns_false_when_element_never_appears(monkeypatch):
    _fast_polling(monkeypatch)
    page = StubPage(rect=None)
    result = asyncio.run(click_element_if_present(page, "#some-overlay"))
    assert result is False
    assert page.click_calls == []


def test_evaluate_error_treated_as_not_present_not_raised(monkeypatch):
    _fast_polling(monkeypatch)
    page = StubPage(raise_on_evaluate=True)
    result = asyncio.run(click_element_if_present(page, "#some-overlay"))
    assert result is False
    assert page.click_calls == []


def test_native_click_rejection_is_not_reported_as_success():
    page = StubPage(rect={"x": 10, "y": 20}, click_result=False)
    result = asyncio.run(click_element_if_present(page, "#some-overlay"))
    assert result is False
    assert page.click_calls == [(10, 20)]
