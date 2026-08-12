"""engine.py's _switch_websdr() reuses the same page/adapter across a site
switch, relying on the new driver's attach() to navigate once its
readiness/handshake check fails against whatever's currently loaded. That
assumption breaks for OpenWebRX: _READY_PREDICATE is true for ANY ready
OpenWebRX instance, not specifically self.url -- so switching from site A
to a different, same-family site B while A is still loaded and ready must
still force a goto(self.url), or attach() silently keeps controlling A."""
import asyncio

from sdrsync.websdr.openwebrx import OpenWebRXDriver


class StubPage:
    def __init__(self, current_url: str, ready: bool = True):
        self.current_url = current_url
        self.ready = ready
        self.goto_calls: list = []
        self.mouse = self

    async def evaluate(self, js, *args):
        if "window.location.href" in js:
            return self.current_url
        # _READY_PREDICATE: whatever page is loaded is a ready OpenWebRX.
        return self.ready

    async def goto(self, url, timeout=None):
        self.goto_calls.append(url)
        self.current_url = url

    async def wait_for_function(self, js, timeout=None):
        return None

    def on(self, event, handler):
        return None


def test_switching_to_a_different_openwebrx_site_forces_navigation():
    # Page is already loaded and ready -- but on a DIFFERENT site than the
    # one we're now attaching to.
    page = StubPage(current_url="https://site-a.example/")
    driver = OpenWebRXDriver("https://site-b.example/")

    asyncio.run(driver.attach(page))

    assert page.goto_calls == ["https://site-b.example/"]
    assert page.current_url == "https://site-b.example/"


def test_reattaching_to_the_same_site_does_not_reload():
    # Same site still loaded and ready (e.g. a profile-change reattach) --
    # must NOT reload, preserving the operator's in-page state.
    page = StubPage(current_url="https://site-a.example/")
    driver = OpenWebRXDriver("https://site-a.example/")

    asyncio.run(driver.attach(page))

    assert page.goto_calls == []
