"""Same bug shape as test_openwebrx_switch_site_forces_reload.py: UberSDR's
_handshake() answers True against ANY live UberSDR v2 bridge, not
specifically self.url -- so engine.py's _switch_websdr() reusing the same
page across a switch to a DIFFERENT UberSDR station could silently skip
navigation and keep controlling the OLD station's receiver. attach() must
gate the reuse-in-place fast path on same_site() before ever trying the
handshake in place.

The stub's wait_for_function always succeeds (whatever page is currently
loaded always answers the agent handshake -- the real-world condition that
makes this bug possible: a previously-installed agent on the OLD site is
still live and ready). What differs pre-/post-fix is whether attach()
tries that in-place handshake at all before deciding to navigate."""
import asyncio

from sdrsync.websdr.ubersdr import UberSDRDriver


class MiniPage:
    """Just enough of the Page surface for attach()'s same-site gate and
    handshake to run -- deliberately not a full protocol double (see
    test_ubersdr_driver.py's StubPage for that). Anything attach() does
    after the handshake gate is allowed to raise; this test only cares
    about what happens before and at that gate."""

    def __init__(self, current_url: str):
        self.current_url = current_url
        self.log: list = []  # ordered: "hello" | ("goto", url)

    async def evaluate(self, js, *args):
        if "window.location.href" in js:
            return self.current_url
        if "send('hello')" in js:
            self.log.append("hello")
        return None

    async def goto(self, url, timeout=None):
        self.log.append(("goto", url))
        self.current_url = url

    async def wait_for_function(self, js, timeout=None):
        return None  # the currently-loaded page always answers ready

    def on(self, event, handler):
        return None


def _run_attach_best_effort(driver, page) -> None:
    """attach() may legitimately raise past the handshake gate against this
    intentionally-partial stub (e.g. _read_descriptor choking on a None
    descriptor) -- irrelevant to what this test checks, so swallow it."""
    try:
        asyncio.run(driver.attach(page))
    except Exception:
        pass


def test_switching_to_a_different_ubersdr_site_navigates_before_trusting_the_handshake():
    page = MiniPage(current_url="https://uber-a.example/v2/")
    driver = UberSDRDriver("https://uber-b.example/")

    _run_attach_best_effort(driver, page)

    goto_events = [e for e in page.log if isinstance(e, tuple) and e[0] == "goto"]
    assert goto_events == [("goto", "https://uber-b.example/v2/")]
    # The in-place handshake must not have been trusted before that goto --
    # if it had (pre-fix behaviour), no navigation would ever have happened.
    assert page.log.index(("goto", "https://uber-b.example/v2/")) == 0


def test_reattaching_to_the_same_ubersdr_site_never_navigates():
    page = MiniPage(current_url="https://uber-a.example/v2/")
    driver = UberSDRDriver("https://uber-a.example/")

    _run_attach_best_effort(driver, page)

    goto_events = [e for e in page.log if isinstance(e, tuple) and e[0] == "goto"]
    assert goto_events == []
    assert "hello" in page.log
