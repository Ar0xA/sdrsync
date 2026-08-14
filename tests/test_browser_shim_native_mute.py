"""Native (page-independent) audio mute -- v15 replaced the old per-site
JS mute() calls (KiwiSDR's toggle_or_set_mute(), etc, confirmed live to add
up to ~1s of perceptible lag depending on each site's own implementation)
with a native WebView-engine-level mute, reached via GetNativeBackend()'s
raw pointer. See browser_shim.py's module comment above
_gtk_native_set_muted() for the full sourcing/verification story.

Covers what's testable without a real WebKitWebView/WebView2 object:
- the GTK/Linux ctypes path's pure GUID-independent pieces are exercised
  live elsewhere (a standalone round-trip script against a real WebView,
  not part of this suite -- see project_brief.md);
- WxPageAdapter.set_muted()'s dispatch/warn-once behavior, and (a real bug
  a bug-hunter review pass caught) that GetNativeBackend() is only ever
  touched from the GUI-thread dispatch, never eagerly at set_muted()'s
  own call time -- against a fake webview and a monkeypatched
  _native_set_muted(), following the same fake-widget pattern as
  test_browser_shim_run_script.py;
- the MSW COM path's pure GUID-parsing helper, cross-checked against the
  real ICoreWebView2_8 IID's known byte layout.
"""
import asyncio

import wx

from sdrsync.websdr import browser_shim
from sdrsync.websdr.browser_shim import WxPageAdapter


class _FakeWebview:
    """Just enough of wx.html2.WebView's surface for WxPageAdapter's
    __init__ and set_muted() -- no real widget, no real WebView2/WebKitGTK
    backend.

    Tracks whether GetNativeBackend() was ever called while the (fake)
    GUI thread wasn't actually pumping -- a real bug caught by a
    bug-hunter review pass: calling a wx widget method off the GUI thread
    can segfault the whole process with no Python exception (reproduced
    live against a real, destroyed WebView), so this fake widget makes
    that specific mistake loud in a test instead of silent."""

    def __init__(self, native_backend=object(), gui_queue: "_GuiThreadQueue | None" = None) -> None:
        self._native_backend = native_backend
        self._gui_queue = gui_queue
        self.get_native_backend_calls_off_gui_thread = 0

    def Bind(self, event, handler) -> None:
        pass

    def AddScriptMessageHandler(self, name: str) -> bool:
        return True

    def AddUserScript(self, js: str) -> bool:
        return True

    def GetNativeBackend(self):
        if self._gui_queue is not None and not self._gui_queue.pumping:
            self.get_native_backend_calls_off_gui_thread += 1
        return self._native_backend


class _GuiThreadQueue:
    """See test_browser_shim_run_script.py's identical helper -- wx.CallAfter
    is monkeypatched to push here instead of running immediately, so each
    test controls exactly when the "GUI thread" dispatch actually runs.
    `pumping` lets _FakeWebview distinguish a call made from inside a
    dispatched callback from one made eagerly at the calling coroutine's
    own await point."""

    def __init__(self) -> None:
        self._pending: list = []
        self.pumping = False

    def push(self, fn, args, kwargs) -> None:
        self._pending.append((fn, args, kwargs))

    def pump(self) -> None:
        self.pumping = True
        try:
            pending, self._pending = self._pending, []
            for fn, args, kwargs in pending:
                fn(*args, **kwargs)
        finally:
            self.pumping = False


def _make_adapter(monkeypatch, loop, native_backend=object()) -> tuple[WxPageAdapter, _FakeWebview, _GuiThreadQueue]:
    gui_queue = _GuiThreadQueue()
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: gui_queue.push(fn, a, kw))
    webview = _FakeWebview(native_backend, gui_queue=gui_queue)
    adapter = WxPageAdapter(webview, loop, already_created=True)
    gui_queue.pump()
    return adapter, webview, gui_queue


def test_set_muted_dispatches_to_native_set_muted_with_the_native_backend(monkeypatch):
    calls = []
    monkeypatch.setattr(browser_shim, "_native_set_muted", lambda native, muted: calls.append((native, muted)) or True)

    async def run():
        loop = asyncio.get_running_loop()
        adapter, webview, gui_queue = _make_adapter(monkeypatch, loop, native_backend="the-native-ptr")
        await adapter.set_muted(True)
        gui_queue.pump()

    asyncio.run(run())

    assert calls == [("the-native-ptr", True)]


def test_get_native_backend_is_only_ever_called_from_the_gui_thread_dispatch(monkeypatch):
    """Regression guard for a real bug caught by a bug-hunter review pass:
    GetNativeBackend() must only run inside the wx.CallAfter-dispatched
    callback (the GUI thread in real life), never eagerly at set_muted()'s
    own call time (the calling asyncio thread) -- calling a wx widget
    method off the GUI thread on an already-Destroy()'d widget segfaults
    the whole process with no Python exception (reproduced live), exactly
    the hazard this module's own docstring says every other widget touch
    here must avoid."""
    monkeypatch.setattr(browser_shim, "_native_set_muted", lambda native, muted: True)

    async def run():
        loop = asyncio.get_running_loop()
        adapter, webview, gui_queue = _make_adapter(monkeypatch, loop, native_backend="the-native-ptr")
        await adapter.set_muted(True)
        # Not yet -- must not have touched the widget before the GUI
        # thread actually runs the dispatched callback.
        assert webview.get_native_backend_calls_off_gui_thread == 0
        gui_queue.pump()
        return webview

    webview = asyncio.run(run())

    assert webview.get_native_backend_calls_off_gui_thread == 0


def test_set_muted_does_nothing_when_native_backend_is_none(monkeypatch):
    calls = []
    monkeypatch.setattr(browser_shim, "_native_set_muted", lambda native, muted: calls.append((native, muted)) or True)

    async def run():
        loop = asyncio.get_running_loop()
        adapter, webview, gui_queue = _make_adapter(monkeypatch, loop, native_backend=None)
        await adapter.set_muted(True)
        gui_queue.pump()

    asyncio.run(run())

    assert calls == []


def test_set_muted_warns_once_not_on_every_call_when_native_mute_is_unavailable(monkeypatch, caplog):
    monkeypatch.setattr(browser_shim, "_native_set_muted", lambda native, muted: False)

    async def run():
        loop = asyncio.get_running_loop()
        adapter, webview, gui_queue = _make_adapter(monkeypatch, loop)
        await adapter.set_muted(True)
        gui_queue.pump()
        await adapter.set_muted(False)
        gui_queue.pump()
        return adapter

    with caplog.at_level("WARNING", logger="sdrsync.websdr.browser_shim"):
        adapter = asyncio.run(run())

    assert adapter._native_mute_warned is True
    warnings = [r for r in caplog.records if "native audio mute unavailable" in r.message]
    assert len(warnings) == 1


def test_native_set_muted_dispatches_by_platform(monkeypatch):
    gtk_calls = []
    win32_calls = []
    monkeypatch.setattr(browser_shim, "_gtk_native_set_muted", lambda native, muted: gtk_calls.append((native, muted)) or True)
    monkeypatch.setattr(browser_shim, "_win32_native_set_muted", lambda native, muted: win32_calls.append((native, muted)) or True)

    monkeypatch.setattr(browser_shim.sys, "platform", "linux")
    browser_shim._native_set_muted("ptr", True)
    assert gtk_calls == [("ptr", True)]
    assert win32_calls == []

    gtk_calls.clear()
    monkeypatch.setattr(browser_shim.sys, "platform", "win32")
    browser_shim._native_set_muted("ptr", False)
    assert win32_calls == [("ptr", False)]
    assert gtk_calls == []


def test_win32_guid_struct_matches_the_real_icorewebview2_8_iid_byte_layout():
    """Cross-checks the GUID-parsing helper against a known-correct byte
    layout for a real, well-documented GUID (ICoreWebView2_8's own IID,
    E9632730-6E1E-43AB-B7B8-7B2C9E62E094 -- extracted directly from
    Microsoft's shipped WebView2.h, see browser_shim.py's module comment)
    -- independent of whether the vtable slot arithmetic elsewhere is
    correct, this at least confirms the GUID itself parses into the exact
    bytes a real COM QueryInterface call expects (mixed-endian: Data1-3
    little-endian, Data4 as-is)."""
    guid = browser_shim._win32_guid_struct("E9632730-6E1E-43AB-B7B8-7B2C9E62E094")
    assert guid.Data1 == 0xE9632730
    assert guid.Data2 == 0x6E1E
    assert guid.Data3 == 0x43AB
    assert bytes(guid.Data4) == bytes.fromhex("B7B87B2C9E62E094")
