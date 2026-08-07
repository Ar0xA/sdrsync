"""wx.html2/WebView2 backend setup -- must run before any wx.App or
WebView is constructed.

Two one-time, startup-only conditions (not runtime failure modes -- see
project_brief.md / the v5 migration plan for why a monitoring thread is
the wrong tool here):

1. wxPython bundles WebView2Loader.dll inside its own package directory
   but does not add that directory to the process's DLL search path
   itself. Without ensure_webview_backend(), wx.html2.WebView silently
   falls back to the legacy IE/Trident backend -- no error raised, pages
   report "loaded" normally, but nothing modern actually runs (confirmed:
   RunScriptAsync never returns anything against it, no error either --
   a completely silent hang-like failure).
2. assert_edge_available() is a hard startup gate. If the DLL-directory
   fix doesn't make the Edge backend available (e.g. the WebView2
   Evergreen Runtime genuinely isn't installed on this machine), the app
   must refuse to start with a clear, actionable message rather than
   silently degrading to IE -- that degradation *is* the failure mode
   this module exists to prevent.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sdrsync.browser.backend")

# os.add_dll_directory() returns a context-manager handle that removes
# the directory from the search path once garbage-collected -- this
# module-global keeps it alive for the whole process lifetime. Do not
# replace this with a bare `os.add_dll_directory(...)` call.
_dll_directory_handle = None

WEBVIEW2_USER_DATA_DIR = Path.home() / ".sdrsync" / "webview2_data"


class EdgeBackendUnavailable(Exception):
    """Raised when wx.html2.WebView cannot use the Edge/WebView2 backend
    even after ensure_webview_backend() has run. Callers must treat this
    as fatal -- there is no acceptable degraded mode (see module
    docstring)."""


_AUTOPLAY_ARG = "--autoplay-policy=no-user-gesture-required"


def ensure_webview_backend() -> None:
    """Idempotent. Call once, before constructing any wx.App or WebView."""
    if sys.platform != "win32":
        # Everything below (DLL search path, WEBVIEW2_* env vars) is
        # Windows/WebView2-specific -- the Linux/macOS backends (WebKitGTK,
        # Cocoa/WebKit) need their own equivalent setup when that follow-up
        # work happens, not this.
        return

    global _dll_directory_handle
    if _dll_directory_handle is None:
        import wx
        wx_dir = os.path.dirname(os.path.abspath(wx.__file__))
        try:
            _dll_directory_handle = os.add_dll_directory(wx_dir)
            logger.debug("Added wx package dir to DLL search path: %s", wx_dir)
        except OSError as e:
            # Don't let this abort before the env-var setup below runs --
            # assert_edge_available() will surface the real failure with a
            # clear message once wx.App() exists.
            logger.warning("Could not add %s to the DLL search path: %s", wx_dir, e)

    WEBVIEW2_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if "WEBVIEW2_USER_DATA_FOLDER" in os.environ:
        logger.warning(
            "WEBVIEW2_USER_DATA_FOLDER is already set (%s) -- leaving it alone rather than "
            "overriding, but this means SDRSync's WebView2 profile isn't isolated from "
            "whatever else set it.", os.environ["WEBVIEW2_USER_DATA_FOLDER"],
        )
    else:
        os.environ["WEBVIEW2_USER_DATA_FOLDER"] = str(WEBVIEW2_USER_DATA_DIR)

    # Matches the autoplay-policy flag the Playwright-based engine already
    # passed to Chromium -- WebSDR sites' own audio-gate JS (see
    # websdr_org.py's _satisfy_audio_gate) still expects/handles a real
    # user-gesture click regardless, this only relaxes the browser-level
    # policy to match what was already true before this migration.
    existing_args = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    if _AUTOPLAY_ARG not in existing_args:
        if existing_args:
            logger.info(
                "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS already set (%r) -- appending our "
                "autoplay flag rather than overwriting it.", existing_args,
            )
            os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"{existing_args} {_AUTOPLAY_ARG}"
        else:
            os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = _AUTOPLAY_ARG


def assert_edge_available() -> None:
    """Call once, right after wx.App() is constructed (IsBackendAvailable
    needs an app instance to exist), before creating any frame/WebView."""
    import wx
    import wx.html2

    if wx.html2.WebView.IsBackendAvailable(wx.html2.WebViewBackendEdge):
        version = _safe_backend_version()
        logger.info("WebView2 (Edge) backend available%s", f", version {version}" if version else "")
        return

    wx_dir = os.path.dirname(os.path.abspath(wx.__file__))
    loader_dll = os.path.join(wx_dir, "WebView2Loader.dll")
    if not os.path.exists(loader_dll):
        raise EdgeBackendUnavailable(
            f"WebView2Loader.dll is missing from the wxPython install ({wx_dir}). "
            "This looks like a broken or unusually old wxPython install -- try "
            "reinstalling wxPython (pip install --force-reinstall wxPython)."
        )
    raise EdgeBackendUnavailable(
        "The Microsoft Edge WebView2 Runtime does not appear to be installed on "
        "this machine (WebView2Loader.dll is present, but no working WebView2 "
        "runtime was found). Install the WebView2 Evergreen Runtime from "
        "https://developer.microsoft.com/microsoft-edge/webview2/ and restart SDRSync. "
        "(On most Windows 10/11 systems this ships with the OS already; this is "
        "expected to be rare.)"
    )


def _safe_backend_version() -> Optional[str]:
    try:
        import wx.html2
        return str(wx.html2.WebView.GetBackendVersionInfo(wx.html2.WebViewBackendEdge))
    except Exception:
        return None
