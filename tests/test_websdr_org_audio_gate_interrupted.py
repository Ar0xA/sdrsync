"""_satisfy_audio_gate() (sdrsync/websdr/websdr_org.py) must call
document.ct.resume() for WebKitGTK's non-standard "interrupted"
AudioContext state, not just the spec's "suspended" -- confirmed live via a
standalone wx.html2.WebView + AudioContext repro on a native (non-WSL2)
Linux desktop: a fresh AudioContext there reports "interrupted", and
resume() is the only thing that clears it, but the pre-fix code only ever
checked for "suspended", so resume() was never called and the WebSDR
stayed silent forever with no error anywhere."""
import asyncio

from sdrsync.websdr import websdr_org
from sdrsync.websdr.websdr_org import WebsdrOrgDriver


class _StubMouse:
    def __init__(self) -> None:
        self.clicked = False

    async def click(self, _x, _y):
        self.clicked = True


class StubPage:
    def __init__(self, state: str) -> None:
        self._state = state
        self.mouse = _StubMouse()
        self.resume_called = False

    async def evaluate(self, js, *_args):
        if "resume" in js:
            self.resume_called = True
            return None
        return self._state


def _run_audio_gate(state: str, monkeypatch) -> StubPage:
    monkeypatch.setattr(websdr_org, "AUDIO_GATE_CHECK_DELAY_S", 0)
    driver = WebsdrOrgDriver("http://example.invalid")
    page = StubPage(state)
    driver._page = page
    asyncio.run(driver._satisfy_audio_gate())
    return page


def test_interrupted_state_triggers_resume(monkeypatch):
    page = _run_audio_gate("interrupted", monkeypatch)
    assert page.resume_called


def test_suspended_state_still_triggers_resume(monkeypatch):
    page = _run_audio_gate("suspended", monkeypatch)
    assert page.resume_called


def test_running_state_does_not_call_resume(monkeypatch):
    page = _run_audio_gate("running", monkeypatch)
    assert not page.resume_called


class _AttachStubPage:
    """Just enough of Page for attach() to complete: goto()/
    wait_for_function() no-op, evaluate() answers the band-table read."""

    def __init__(self) -> None:
        self.mouse = _StubMouse()

    def on(self, event, handler):
        pass

    async def goto(self, url, timeout=None):
        pass

    async def wait_for_function(self, js, timeout=None):
        pass

    async def evaluate(self, js, *_args):
        return [{"c": 14074, "s": 12}]  # one band, keeps _load_band_table() happy


def test_audio_gate_click_is_skipped_on_windows(monkeypatch):
    """Windows/Edge WebView2 already runs with
    --autoplay-policy=no-user-gesture-required (browser/backend.py) --
    this driver's own click was NOT platform-gated (unlike KiwiSDR's/
    OpenWebRX's identical watchers), found by a bug-hunter review pass:
    it moved the user's real mouse on every Windows attach for nothing,
    and contradicted AppSettings.auto_click_audio_unlock's own docstring
    ("No effect on Windows/WebView2")."""
    monkeypatch.setattr(websdr_org.sys, "platform", "win32")
    driver = WebsdrOrgDriver("http://example.invalid")
    gate_calls = []
    driver._satisfy_audio_gate = lambda: gate_calls.append(1) or _immediate()
    page = _AttachStubPage()

    asyncio.run(driver.attach(page))

    assert gate_calls == []


def test_audio_gate_click_still_happens_off_windows(monkeypatch):
    monkeypatch.setattr(websdr_org.sys, "platform", "linux")
    driver = WebsdrOrgDriver("http://example.invalid")
    gate_calls = []
    driver._satisfy_audio_gate = lambda: gate_calls.append(1) or _immediate()
    page = _AttachStubPage()

    asyncio.run(driver.attach(page))

    assert gate_calls == [1]


async def _immediate():
    return None
