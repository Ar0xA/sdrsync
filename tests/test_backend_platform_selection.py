"""target_backend() must return the right wx.html2 backend constant for
each platform -- win32 gets Edge, everything else gets WebKit (the one
non-Windows constant, covering both WebKitGTK on Linux and Cocoa/WKWebView
on macOS -- see the 2026-08-07 Linux spike in project_brief.md)."""
import sys

import pytest
import wx.html2

from sdrsync.browser import backend


def test_target_backend_win32(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    if not hasattr(wx.html2, "WebViewBackendEdge"):
        # wx's GTK build doesn't define this constant at all (unlike
        # WebViewBackendWebKit, which is universal) -- can't run this
        # assertion on a non-Windows wxPython install regardless of the
        # sys.platform monkeypatch above.
        pytest.skip("WebViewBackendEdge not defined on this wx build")
    assert backend.target_backend() == wx.html2.WebViewBackendEdge


def test_target_backend_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert backend.target_backend() == wx.html2.WebViewBackendWebKit


def test_target_backend_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert backend.target_backend() == wx.html2.WebViewBackendWebKit
