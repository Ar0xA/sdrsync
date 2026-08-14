"""KiwiSDRDriver.attach() must click .id-play-button-container when it's
present -- confirmed live in KiwiSDR's own JS (test_audio_suspended() in
kiwisdr.min.js) that this overlay's onclick is the ONLY thing that ever
calls AudioContext.resume() on that page. Regression coverage for the real
bug: on WebKitGTK (native Linux desktop, non-WSL2), a fresh AudioContext
isn't auto-"running", the overlay genuinely renders, and sdrsync never
clicked it -- so tuning/mode control worked but audio stayed silent
forever with no error anywhere. (Selector is a CSS class despite the
"id-" prefix in its name -- an ID-selector version of this same bug fix
shipped first and was live-confirmed to never match anything real.)

The click runs from a background task that retries for as long as the
driver stays attached (see attach()'s own comment: a real observed case
had the overlay still not present a full 20s after session start, well
past what seemed like a generous one-shot window), so these tests
explicitly await driver._audio_unlock_task after attach() returns -- a
bare asyncio.run() around attach() alone would tear the loop down before
that task ever got to run.

The watcher itself is gated off entirely on win32 (see attach()'s own
`sys.platform != "win32"` check -- WebView2 never needs it). Tests that
exercise the watcher actually clicking are marked _NOT_WIN32 below, so
running this file for real on Windows reports them as SKIPPED rather
than FAILED -- they're testing functionality that's intentionally
disabled there, not a cross-platform regression. Only
test_skips_the_click_entirely_on_windows and
test_skips_the_click_when_disabled_via_settings are meant to run on
every host OS (they verify the gating logic itself, which must hold
regardless of what platform pytest happens to run on)."""
import asyncio
import sys

import pytest

from sdrsync.websdr import browser_shim
from sdrsync.websdr import kiwisdr as kiwisdr_module
from sdrsync.websdr.kiwisdr import KiwiSDRDriver

_NOT_WIN32 = pytest.mark.skipif(
    sys.platform == "win32", reason="audio-unlock click watcher is disabled entirely on win32"
)


class StubPage:
    """overlay_rect=None means the overlay never appears. Otherwise it's
    present until clicked, at which point it disappears (the ordinary
    case) -- unless never_disappears=True, simulating KiwiSDR's
    require_id receivers, where the overlay's onclick='' means clicking
    it is a genuine no-op and it stays right where it was."""

    def __init__(self, overlay_rect=None, never_disappears=False):
        self._overlay_rect = overlay_rect
        self._never_disappears = never_disappears
        self._clicked = False
        self.click_calls: list = []
        self.mouse = self

    async def goto(self, url, timeout=None):
        return None

    async def wait_for_function(self, js, timeout=None):
        return None

    def on(self, event, handler):
        return None

    async def evaluate(self, js, *args):
        # attach() itself never calls evaluate() directly -- the only
        # evaluate() calls during attach() are click_element_if_present()'s
        # and element_is_present()'s own selector probes.
        assert "querySelector" in js
        if self._clicked and not self._never_disappears:
            return None
        return self._overlay_rect

    async def click(self, x, y):
        self._clicked = True
        self.click_calls.append((x, y))


async def _attach_and_wait_for_watcher(driver: KiwiSDRDriver, page: StubPage) -> None:
    await driver.attach(page)
    if driver._audio_unlock_task is not None:
        await driver._audio_unlock_task


@_NOT_WIN32
def test_clicks_overlay_when_present(monkeypatch):
    monkeypatch.setattr(kiwisdr_module, "AUDIO_UNLOCK_OVERLAY_FADE_S", 0.01)
    page = StubPage(overlay_rect={"x": 50.0, "y": 60.0})
    driver = KiwiSDRDriver("http://example.invalid")

    asyncio.run(_attach_and_wait_for_watcher(driver, page))

    assert page.click_calls == [(50, 60)]
    assert driver.attached is True


@_NOT_WIN32
def test_keeps_watching_when_click_was_a_no_op(monkeypatch):
    # Regression coverage: a receiver with cfg.require_id set renders the
    # exact same overlay class with onclick='' (an ID/callsign text field
    # takes its place instead) until the operator types something in --
    # confirmed live in KiwiSDR's own JS. A dispatched click there is a
    # genuine no-op, so the overlay never disappears; the watcher must not
    # mistake "a click was sent" for "it worked" and stop watching.
    monkeypatch.setattr(kiwisdr_module, "AUDIO_UNLOCK_OVERLAY_FADE_S", 0.01)
    monkeypatch.setattr(kiwisdr_module, "AUDIO_UNLOCK_WATCH_ATTEMPT_S", 0.02)
    monkeypatch.setattr(browser_shim, "CLICK_ELEMENT_POLL_INTERVAL_S", 0.01)
    page = StubPage(overlay_rect={"x": 50.0, "y": 60.0}, never_disappears=True)
    driver = KiwiSDRDriver("http://example.invalid")

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

    # It kept clicking (the no-op button, every retry) rather than
    # stopping after the first click and falsely declaring success.
    assert len(page.click_calls) > 1


@_NOT_WIN32
def test_does_not_click_when_overlay_absent(monkeypatch):
    # e.g. Windows/WebView2, where sdrsync's own --autoplay-policy flag
    # means the overlay never renders in the first place. The watcher only
    # gives up when the driver is closed (by design, since a fixed window
    # proved unreliable -- see module docstring), so this simulates the
    # session ending after a few retry attempts rather than waiting for a
    # loop that would otherwise never exit on its own.
    monkeypatch.setattr(kiwisdr_module, "AUDIO_UNLOCK_WATCH_ATTEMPT_S", 0.02)
    monkeypatch.setattr(browser_shim, "CLICK_ELEMENT_POLL_INTERVAL_S", 0.01)
    page = StubPage(overlay_rect=None)
    driver = KiwiSDRDriver("http://example.invalid")

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
    # appear. A page that WOULD find the overlay (evaluate() below would
    # assert-fail if ever called) proves the win32 branch skips the
    # watcher outright, not just happening to find nothing.
    monkeypatch.setattr(sys, "platform", "win32")

    class PageThatMustNotBeQueried(StubPage):
        async def evaluate(self, js, *args):
            raise AssertionError("click_element_if_present() must not run on win32")

    page = PageThatMustNotBeQueried(overlay_rect={"x": 50.0, "y": 60.0})
    driver = KiwiSDRDriver("http://example.invalid")

    asyncio.run(driver.attach(page))

    assert driver._audio_unlock_task is None
    assert page.click_calls == []
    assert driver.attached is True


def test_skips_the_click_when_disabled_via_settings():
    # Behaviour panel's "Mouse hijack to enable WebSDR audio" checkbox --
    # off means the operator will click any Start/Play control themselves.
    class PageThatMustNotBeQueried(StubPage):
        async def evaluate(self, js, *args):
            raise AssertionError("click_element_if_present() must not run when disabled")

    page = PageThatMustNotBeQueried(overlay_rect={"x": 50.0, "y": 60.0})
    driver = KiwiSDRDriver("http://example.invalid", auto_click_audio_unlock=False)

    asyncio.run(driver.attach(page))

    assert driver._audio_unlock_task is None
    assert page.click_calls == []
    assert driver.attached is True


@_NOT_WIN32
def test_stops_clicking_after_give_up_threshold_on_a_require_id_receiver(monkeypatch):
    """Found by a bug-hunter review pass: with no cap, a require_id
    receiver (every click a permanent no-op) made the watcher click
    forever -- a real OS-level mouse click roughly every
    AUDIO_UNLOCK_WATCH_ATTEMPT_S + AUDIO_UNLOCK_OVERLAY_FADE_S seconds,
    indefinitely, actively hijacking the cursor the operator needs to
    type their ID into the very page this was trying to help with. After
    AUDIO_UNLOCK_CLICK_GIVE_UP_AFTER ineffective clicks, it must stop
    clicking (while still watching, in case the operator clears the
    overlay themselves)."""
    monkeypatch.setattr(kiwisdr_module, "AUDIO_UNLOCK_OVERLAY_FADE_S", 0.01)
    monkeypatch.setattr(kiwisdr_module, "AUDIO_UNLOCK_WATCH_ATTEMPT_S", 0.02)
    monkeypatch.setattr(kiwisdr_module, "AUDIO_UNLOCK_PROBE_ONLY_INTERVAL_S", 0.02)
    monkeypatch.setattr(browser_shim, "CLICK_ELEMENT_POLL_INTERVAL_S", 0.01)
    page = StubPage(overlay_rect={"x": 50.0, "y": 60.0}, never_disappears=True)
    driver = KiwiSDRDriver("http://example.invalid")

    async def _run():
        await driver.attach(page)
        await asyncio.sleep(0.3)  # well past AUDIO_UNLOCK_CLICK_GIVE_UP_AFTER clicks
        count_after_giving_up = len(page.click_calls)
        await asyncio.sleep(0.3)  # probe-only phase -- must not click any more
        task = driver._audio_unlock_task
        await driver.close()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
        return count_after_giving_up

    count_after_giving_up = asyncio.run(_run())

    assert count_after_giving_up == kiwisdr_module.AUDIO_UNLOCK_CLICK_GIVE_UP_AFTER
    assert len(page.click_calls) == count_after_giving_up, "must not click again during the probe-only phase"


@_NOT_WIN32
def test_probe_only_phase_reports_success_if_the_overlay_clears_itself(monkeypatch):
    """Once the watcher has given up clicking, it should still notice the
    operator (or the page itself) clearing the overlay some other way,
    and stop watching -- not spin forever."""
    monkeypatch.setattr(kiwisdr_module, "AUDIO_UNLOCK_OVERLAY_FADE_S", 0.01)
    monkeypatch.setattr(kiwisdr_module, "AUDIO_UNLOCK_WATCH_ATTEMPT_S", 0.02)
    monkeypatch.setattr(kiwisdr_module, "AUDIO_UNLOCK_PROBE_ONLY_INTERVAL_S", 0.02)
    monkeypatch.setattr(browser_shim, "CLICK_ELEMENT_POLL_INTERVAL_S", 0.01)
    page = StubPage(overlay_rect={"x": 50.0, "y": 60.0}, never_disappears=True)
    driver = KiwiSDRDriver("http://example.invalid")

    async def _run():
        await driver.attach(page)
        # Let it give up clicking (AUDIO_UNLOCK_CLICK_GIVE_UP_AFTER clicks).
        await asyncio.sleep(0.3)
        assert len(page.click_calls) == kiwisdr_module.AUDIO_UNLOCK_CLICK_GIVE_UP_AFTER
        # Operator clears it themselves, with no further click from us.
        page._never_disappears = False
        page._clicked = True
        task = driver._audio_unlock_task
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_run())

    assert len(page.click_calls) == kiwisdr_module.AUDIO_UNLOCK_CLICK_GIVE_UP_AFTER
