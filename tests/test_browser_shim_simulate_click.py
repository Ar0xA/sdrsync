"""WxPageAdapter._simulate_click() -- a real OS-level click via
wx.UIActionSimulator, used to satisfy WebSDR pages' autoplay-gesture
requirement.

Regression coverage for a real bug caught by a bug-hunter review pass:
the click was dispatched with no check that the target screen position
was actually under our own window first. ClientToScreen() happily
returns coordinates for a window that isn't visible there at all (e.g.
minimized/iconized), so a minimized host frame, or anything else
currently covering that exact point (another app's window, a dialog),
still got a real OS-level click sent to it. Fixed by confirming
wx.FindWindowAtPoint() at that screen position resolves back to our own
top-level window (and that window is actually shown/not iconized)
before ever touching wx.UIActionSimulator."""
import asyncio

import wx

from sdrsync.websdr.browser_shim import WxPageAdapter


class _FakeTopLevel:
    def __init__(self, shown: bool = True, iconized: bool = False) -> None:
        self._shown = shown
        self._iconized = iconized

    def IsShown(self) -> bool:
        return self._shown

    def IsIconized(self) -> bool:
        return self._iconized


class _FakeWebview:
    def __init__(self, top_level: "_FakeTopLevel | None") -> None:
        self._top_level = top_level

    def Bind(self, *args, **kwargs) -> None:
        pass

    def AddScriptMessageHandler(self, name: str) -> bool:
        return True

    def AddUserScript(self, js: str) -> bool:
        return True

    def ClientToScreen(self, point: wx.Point) -> wx.Point:
        return wx.Point(point.x + 1000, point.y + 2000)  # arbitrary screen offset

    def GetTopLevelParent(self):
        return self._top_level


class _FakeSimulator:
    """Records calls instead of touching the real OS mouse -- these
    tests must never actually move the cursor."""

    instances: list = []

    def __init__(self) -> None:
        self.calls: list = []
        _FakeSimulator.instances.append(self)

    def MouseMove(self, point) -> None:
        self.calls.append(("move", point.x, point.y))

    def MouseClick(self, button) -> None:
        self.calls.append(("click", button))


def _make_adapter(monkeypatch, loop, top_level) -> WxPageAdapter:
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))  # runs synchronously
    monkeypatch.setattr(wx, "SafeYield", lambda: None)
    monkeypatch.setattr(wx, "MilliSleep", lambda ms: None)
    monkeypatch.setattr(wx, "UIActionSimulator", _FakeSimulator)
    # _simulate_click() enforces "never called from the GUI thread" (a
    # real, separate invariant of its own, unrelated to what these tests
    # cover) -- these tests run on pytest's own thread the same way
    # production code calls it from SyncEngine's background asyncio
    # thread, never wx's GUI thread, so IsMainThread() must read False
    # here the same way it would there.
    monkeypatch.setattr(wx, "IsMainThread", lambda: False)
    _FakeSimulator.instances = []
    webview = _FakeWebview(top_level)
    adapter = WxPageAdapter(webview, loop, already_created=True)
    adapter._on_screen_presenter = lambda shown: None
    return adapter


def test_click_is_skipped_when_no_window_of_ours_is_at_that_point(monkeypatch):
    """wx.FindWindowAtPoint() only ever returns a window belonging to
    this process -- None here means something else (another app, a
    screensaver, empty desktop) currently occupies that exact screen
    position."""
    monkeypatch.setattr(wx, "FindWindowAtPoint", lambda pos: None)

    async def run():
        loop = asyncio.get_running_loop()
        adapter = _make_adapter(monkeypatch, loop, _FakeTopLevel())
        await adapter._simulate_click(5, 5)

    asyncio.run(run())

    assert _FakeSimulator.instances == [] or all(
        sim.calls == [] for sim in _FakeSimulator.instances
    ), "must not touch the real mouse when nothing of ours is at that point"


def test_click_is_skipped_when_the_point_belongs_to_a_different_top_level_window(monkeypatch):
    """The point resolves to SOME window of ours, but not the one this
    click is meant for -- e.g. another one of the app's own top-level
    windows happens to be covering it."""
    our_top_level = _FakeTopLevel()
    other_top_level = _FakeTopLevel()

    class _FoundElsewhere:
        def GetTopLevelParent(self):
            return other_top_level

    monkeypatch.setattr(wx, "FindWindowAtPoint", lambda pos: _FoundElsewhere())

    async def run():
        loop = asyncio.get_running_loop()
        adapter = _make_adapter(monkeypatch, loop, our_top_level)
        await adapter._simulate_click(5, 5)

    asyncio.run(run())

    assert all(sim.calls == [] for sim in _FakeSimulator.instances)


def test_click_is_skipped_when_the_host_window_is_iconized(monkeypatch):
    top_level = _FakeTopLevel(shown=True, iconized=True)

    class _FoundOurs:
        def GetTopLevelParent(self):
            return top_level

    monkeypatch.setattr(wx, "FindWindowAtPoint", lambda pos: _FoundOurs())

    async def run():
        loop = asyncio.get_running_loop()
        adapter = _make_adapter(monkeypatch, loop, top_level)
        await adapter._simulate_click(5, 5)

    asyncio.run(run())

    assert all(sim.calls == [] for sim in _FakeSimulator.instances)


def test_click_proceeds_when_the_point_is_genuinely_under_our_own_window(monkeypatch):
    top_level = _FakeTopLevel(shown=True, iconized=False)

    class _FoundOurs:
        def GetTopLevelParent(self):
            return top_level

    monkeypatch.setattr(wx, "FindWindowAtPoint", lambda pos: _FoundOurs())

    async def run():
        loop = asyncio.get_running_loop()
        adapter = _make_adapter(monkeypatch, loop, top_level)
        await adapter._simulate_click(5, 5)

    asyncio.run(run())

    assert len(_FakeSimulator.instances) == 1
    assert ("click", wx.MOUSE_BTN_LEFT) in _FakeSimulator.instances[0].calls


def test_native_click_exception_returns_false(monkeypatch):
    top_level = _FakeTopLevel(shown=True, iconized=False)

    class _FoundOurs:
        def GetTopLevelParent(self):
            return top_level

    class _FailingSimulator(_FakeSimulator):
        def MouseClick(self, button) -> None:
            raise RuntimeError("native injection failed")

    monkeypatch.setattr(wx, "FindWindowAtPoint", lambda pos: _FoundOurs())

    async def run():
        loop = asyncio.get_running_loop()
        adapter = _make_adapter(monkeypatch, loop, top_level)
        monkeypatch.setattr(wx, "UIActionSimulator", _FailingSimulator)
        return await adapter._simulate_click(5, 5)

    assert asyncio.run(run()) is False
