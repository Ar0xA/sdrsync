"""OpenWebRXDriver.attach() must click #openwebrx-autoplay-overlay when
it's present, on a fresh navigation only -- confirmed live in OpenWebRX's
own JS that AudioEngine only starts once its AudioContext reaches
"running", and the overlay's click handler is the only thing that ever
calls audioEngine.resume() on that page. Regression coverage for the real
bug: on WebKitGTK (native Linux desktop, non-WSL2), a fresh AudioContext
isn't auto-"running", the overlay genuinely renders, and sdrsync never
clicked it -- so tuning/mode control worked but audio stayed silent
forever with no error anywhere.

The click runs from a background task that retries for as long as the
driver stays attached (see attach()'s own comment, same reasoning as
kiwisdr.py's identical watcher), so these tests explicitly await
driver._audio_unlock_task after attach() returns -- a bare asyncio.run()
around attach() alone would tear the loop down before that task ever got
to run.

The watcher itself is gated off entirely on win32 -- see kiwisdr.py's
identical test file's module docstring for why tests that exercise the
watcher actually clicking are marked _NOT_WIN32 (skip cleanly on
Windows, rather than failing)."""
import asyncio
import sys

import pytest

from sdrsync.websdr import browser_shim
from sdrsync.websdr import openwebrx as openwebrx_module
from sdrsync.websdr.openwebrx import OpenWebRXDriver

_NOT_WIN32 = pytest.mark.skipif(
    sys.platform == "win32", reason="audio-unlock click watcher is disabled entirely on win32"
)


class StubPage:
    """overlay_rect=None means the overlay never appears. Otherwise it's
    present until clicked, at which point it disappears (the ordinary
    case) -- unless never_disappears=True."""

    def __init__(self, overlay_rect=None, never_disappears=False):
        self._overlay_rect = overlay_rect
        self._never_disappears = never_disappears
        self._clicked = False
        self.click_calls: list = []
        self.goto_calls: list = []
        self.mouse = self

    async def goto(self, url, timeout=None):
        self.goto_calls.append(url)

    async def wait_for_function(self, js, timeout=None):
        return None

    def on(self, event, handler):
        return None

    async def evaluate(self, js, *args):
        if "window.location.href" in js:
            # Fresh page, nothing loaded yet -- forces goto() (matches
            # test_openwebrx_switch_site_forces_reload.py's own StubPage
            # convention for an empty/never-navigated page).
            return ""
        if "querySelector" in js:
            if self._clicked and not self._never_disappears:
                return None
            return self._overlay_rect
        # _READY_PREDICATE, only reached if already_ready's own check
        # short-circuited to True, which it never does here (blank
        # current_url never same_site()-matches self.url).
        return False

    async def click(self, x, y):
        self._clicked = True
        self.click_calls.append((x, y))


async def _attach_and_wait_for_watcher(driver: OpenWebRXDriver, page: StubPage) -> None:
    await driver.attach(page)
    if driver._audio_unlock_task is not None:
        await driver._audio_unlock_task


@_NOT_WIN32
def test_clicks_overlay_when_present(monkeypatch):
    monkeypatch.setattr(openwebrx_module, "AUDIO_UNLOCK_OVERLAY_FADE_S", 0.01)
    page = StubPage(overlay_rect={"x": 50.0, "y": 60.0})
    driver = OpenWebRXDriver("http://example.invalid")

    asyncio.run(_attach_and_wait_for_watcher(driver, page))

    assert page.click_calls == [(50, 60)]
    assert driver.attached is True


@_NOT_WIN32
def test_keeps_watching_when_click_was_a_no_op(monkeypatch):
    # A dispatched click is not proof it did anything -- see kiwisdr.py's
    # identical watcher/test for the confirmed real-world case this
    # guards against. If the overlay never disappears after being
    # clicked, the watcher must not mistake that for success.
    monkeypatch.setattr(openwebrx_module, "AUDIO_UNLOCK_OVERLAY_FADE_S", 0.01)
    monkeypatch.setattr(openwebrx_module, "AUDIO_UNLOCK_WATCH_ATTEMPT_S", 0.02)
    monkeypatch.setattr(browser_shim, "CLICK_ELEMENT_POLL_INTERVAL_S", 0.01)
    page = StubPage(overlay_rect={"x": 50.0, "y": 60.0}, never_disappears=True)
    driver = OpenWebRXDriver("http://example.invalid")

    async def _run():
        await driver.attach(page)
        await asyncio.sleep(0.2)  # let a few retry attempts happen
        task = driver._audio_unlock_task
        await driver.close()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())

    assert len(page.click_calls) > 1


@_NOT_WIN32
def test_does_not_click_when_overlay_absent(monkeypatch):
    # e.g. Windows/WebView2, where sdrsync's own --autoplay-policy flag
    # means the overlay never renders in the first place. The watcher only
    # gives up when the driver is closed (by design, since a fixed window
    # proved unreliable -- see module docstring), so this simulates the
    # session ending after a few retry attempts rather than waiting for a
    # loop that would otherwise never exit on its own.
    monkeypatch.setattr(openwebrx_module, "AUDIO_UNLOCK_WATCH_ATTEMPT_S", 0.02)
    monkeypatch.setattr(browser_shim, "CLICK_ELEMENT_POLL_INTERVAL_S", 0.01)
    page = StubPage(overlay_rect=None)
    driver = OpenWebRXDriver("http://example.invalid")

    async def _run():
        await driver.attach(page)
        await asyncio.sleep(0.15)  # let a few retry attempts happen
        task = driver._audio_unlock_task
        await driver.close()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())

    assert page.click_calls == []


def test_skips_the_click_entirely_on_windows(monkeypatch):
    # Regression coverage: v2.2.6 already worked fine on Windows (the
    # overlay never renders there -- see module docstring), so this must
    # not spend any time watching for an element that can provably never
    # appear. A page that WOULD find the overlay if queried proves the
    # win32 branch skips the watcher outright, not just happening to find
    # nothing.
    monkeypatch.setattr(sys, "platform", "win32")

    class PageThatMustNotBeQueried(StubPage):
        async def evaluate(self, js, *args):
            if "querySelector" in js:
                raise AssertionError("click_element_if_present() must not run on win32")
            return await super().evaluate(js, *args)

    page = PageThatMustNotBeQueried(overlay_rect={"x": 50.0, "y": 60.0})
    driver = OpenWebRXDriver("http://example.invalid")

    asyncio.run(driver.attach(page))

    assert driver._audio_unlock_task is None
    assert page.click_calls == []
    assert driver.attached is True


def test_skips_the_click_when_disabled_via_settings():
    # Behaviour panel's "Mouse hijack to enable WebSDR audio" checkbox --
    # off means the operator will click any Start/Play control themselves.
    class PageThatMustNotBeQueried(StubPage):
        async def evaluate(self, js, *args):
            if "querySelector" in js:
                raise AssertionError("click_element_if_present() must not run when disabled")
            return await super().evaluate(js, *args)

    page = PageThatMustNotBeQueried(overlay_rect={"x": 50.0, "y": 60.0})
    driver = OpenWebRXDriver("http://example.invalid", auto_click_audio_unlock=False)

    asyncio.run(driver.attach(page))

    assert driver._audio_unlock_task is None
    assert page.click_calls == []
    assert driver.attached is True


@_NOT_WIN32
def test_stops_clicking_after_give_up_threshold(monkeypatch):
    """See kiwisdr.py's identical test -- a bug-hunter review pass found
    the unbounded version could click forever. After
    AUDIO_UNLOCK_CLICK_GIVE_UP_AFTER ineffective clicks, it must stop
    clicking (while still watching)."""
    monkeypatch.setattr(openwebrx_module, "AUDIO_UNLOCK_OVERLAY_FADE_S", 0.01)
    monkeypatch.setattr(openwebrx_module, "AUDIO_UNLOCK_WATCH_ATTEMPT_S", 0.02)
    monkeypatch.setattr(openwebrx_module, "AUDIO_UNLOCK_PROBE_ONLY_INTERVAL_S", 0.02)
    monkeypatch.setattr(browser_shim, "CLICK_ELEMENT_POLL_INTERVAL_S", 0.01)
    page = StubPage(overlay_rect={"x": 50.0, "y": 60.0}, never_disappears=True)
    driver = OpenWebRXDriver("http://example.invalid")

    async def _run():
        await driver.attach(page)
        await asyncio.sleep(0.3)
        count_after_giving_up = len(page.click_calls)
        await asyncio.sleep(0.3)
        task = driver._audio_unlock_task
        await driver.close()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
        return count_after_giving_up

    count_after_giving_up = asyncio.run(_run())

    assert count_after_giving_up == openwebrx_module.AUDIO_UNLOCK_CLICK_GIVE_UP_AFTER
    assert len(page.click_calls) == count_after_giving_up, "must not click again during the probe-only phase"
