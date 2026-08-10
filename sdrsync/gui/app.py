"""wxPython control panel (replaces the earlier Tkinter GUI -- see the v5
migration plan for why: Playwright was replaced with an embedded
wx.html2.WebView, which requires being hosted inside a wxPython app since
pywebview's alternative unconditionally demands the process main thread,
conflicting with Tkinter already owning it).

Thread contract:
    - wx owns the main thread.
    - The asyncio stack (rigctld + SyncEngine + WxPageAdapter dispatch)
      runs in one background thread, for the whole app session (started
      once at startup, joined once when the window closes) via
      asyncio.run(engine.run()).
    - Status flows background -> GUI only through a queue.Queue, drained by
      a wx.Timer on the GUI thread. No wx widget is ever touched from the
      background thread directly (SyncEngine's WebViewHost handles its own
      GUI-thread dispatch for WebView creation/destruction internally).
    - GUI -> background commands go through SyncEngine's thread-safe entry
      points: stop_from_other_thread() (whole-session shutdown, window
      closing) and the independent per-subsystem
      start_rig_from_other_thread()/stop_rig_from_other_thread()/
      start_websdr_from_other_thread()/stop_websdr_from_other_thread() --
      the rig and WebSDR connections have separate lifecycles (picking a
      different SDR has nothing to do with the transceiver connection, and
      vice versa), each with its own Connect/Disconnect button in its own
      panel below.
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

import wx

from sdrsync import __version__
from sdrsync.browser.backend import WebViewBackendUnavailable, assert_backend_available, ensure_webview_backend
from sdrsync.config import (
    MAX_POLL_INTERVAL_S,
    MIN_POLL_INTERVAL_S,
    AppSettings,
    KNOWN_SITES,
    WebSDRSite,
    clamp_idle_disconnect_min,
    clamp_reverse_sync_bounds,
)
from sdrsync.gui_messages import GuiMessage
from sdrsync.gui import theme
from sdrsync.gui.fonts import load_fonts
from sdrsync.gui.receiver_host import ReceiverHost
from sdrsync.gui.section_bar import SectionBar
from sdrsync.gui.settings_host import SettingsHost
from sdrsync.gui.settings_panels.behaviour_panel import BehaviourPanel
from sdrsync.gui.settings_panels.sites_panel import SitesPanel
from sdrsync.gui.settings_panels.transceiver_panel import TransceiverPanel
from sdrsync.gui.state import AppState
from sdrsync.gui.strip_panel import StripPanel
from sdrsync.gui.site_manager_dialog import SiteManagerDialog
from sdrsync.gui.webview_host import WebViewHost, bring_pair_to_front, has_display_at
from sdrsync.logging_setup import LOG_FILE
from sdrsync.resources import ICON_PATH
from sdrsync.preflight import (
    DetectResult,
    RigPreflightResult,
    WebsdrPreflightResult,
    check_flrig,
    check_rigctld,
    check_websdr_url,
    detect_websdr_type,
)
from sdrsync.sitesource import CURATED_LIST_URL, SiteListFetchResult
from sdrsync.sync.engine import StatusSnapshot, SyncEngine

logger = logging.getLogger("sdrsync.gui")

QUEUE_POLL_MS = 150
SHUTDOWN_TIMEOUT_S = 5.0
CUSTOM_URL_SENTINEL = "Custom URL..."
LABEL_WRAP_PX = 380
ERROR_COLOUR = wx.Colour(200, 0, 0)
# Fixed display order for the Backend dropdown -- config.RIG_BACKENDS is
# a set (used for validation membership checks), which has no defined
# order.
RIG_BACKEND_CHOICES = ["rigctld", "flrig"]


def _set_wrapped(static_text: wx.StaticText, text: str, width: int = LABEL_WRAP_PX) -> None:
    static_text.SetLabel(text)
    static_text.Wrap(width)


def _parse_optional_hz(text: str) -> tuple[Optional[int], bool]:
    """Parse a reverse-sync range field, returning (value, is_valid).

    Blank -> (None, True): unrestricted is an intentional, valid state,
    not an error. Anything non-numeric -> (None, False), reported as
    INVALID rather than silently collapsing to unrestricted -- this field
    bounds what a WebSDR page may retune a real transmitter to, so a
    typo must never be read as "no bound at all". See
    _on_reverse_sync_range_commit() for what the caller does with it."""
    text = text.strip()
    if not text:
        return None, True
    try:
        return int(text), True
    except ValueError:
        return None, False


def _parse_optional_minutes(text: str) -> tuple[Optional[int], bool]:
    """Parse the idle-disconnect field, returning (value, is_valid).

    Blank -> (None, True): "never idle-disconnect" is an intentional,
    valid state. Anything non-numeric -> (None, False), reported as
    INVALID and reverted rather than silently persisted -- same rule as
    _parse_optional_hz, for the same reason (what's on screen must be
    what's actually in force). 0 parses fine and also means disabled; see
    config.AppSettings.websdr_idle_disconnect_min."""
    text = text.strip()
    if not text:
        return None, True
    try:
        return int(text), True
    except ValueError:
        return None, False


class _Tooltip:
    """Minimal dynamic tooltip. text_func() is re-evaluated on every hover
    so it can explain a *currently disabled* control (e.g. why a button
    won't click); returning None/"" shows nothing. Uses wx's built-in
    tooltip mechanism (SetToolTip), just refreshed right before it would
    be shown."""

    def __init__(self, widget: wx.Window, text_func: Callable[[], Optional[str]]) -> None:
        self._widget = widget
        self._text_func = text_func
        widget.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)

    def _on_enter(self, event: wx.MouseEvent) -> None:
        text = self._text_func()
        self._widget.SetToolTip(text if text else None)
        event.Skip()


class _PlaceholderBand(wx.Panel):
    """GUI REWRITE IN PROGRESS: stands in for a not-yet-built chrome band
    (spec §2's StripPanel/SectionBar/SettingsHost/ReceiverHost/
    StatusBarPanel) during phase 3, so the frame skeleton's geometry can
    be verified before the real widgets exist. Each later phase replaces
    exactly one of these with its real module; none should remain by
    phase 10."""

    def __init__(self, parent: wx.Window, name: str, height: Optional[int] = None,
                 fill: wx.Colour = theme.SURFACE, hairline_edge: str = "bottom") -> None:
        style = wx.TAB_TRAVERSAL
        super().__init__(parent, style=style, size=wx.Size(-1, height) if height else wx.DefaultSize)
        self._name = name
        self._fill = fill
        self._hairline_edge = hairline_edge
        if height is not None:
            self.SetMinSize(wx.Size(-1, self.FromDIP(height)))
        self.SetBackgroundColour(fill)
        self.Bind(wx.EVT_PAINT, self._on_paint)

    def _on_paint(self, _evt: wx.PaintEvent) -> None:
        dc = wx.BufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self._fill))
        dc.Clear()
        dc.SetFont(wx.Font(wx.FontInfo(9)))
        dc.SetTextForeground(theme.MUTED)
        dc.DrawText(self._name, self.FromDIP(8), self.FromDIP(4))
        theme.draw_hairline(dc, self.GetClientRect(), self._hairline_edge)


class MainFrame(wx.Frame):
    def __init__(self, settings: AppSettings, webview_host: WebViewHost) -> None:
        # spec §2: a genuinely resizable frame at a fixed default/min size
        # -- the receiver band (ReceiverHost, proportion=1) absorbs all
        # extra space on resize/maximize, replacing the old
        # compute-size-from-content model entirely (no more
        # _resize_main_window_to_content()/_finish_initial_layout()).
        load_fonts()
        super().__init__(
            None, title=f"SDRSync {__version__} - not connected",
            style=wx.DEFAULT_FRAME_STYLE, size=theme.FRAME_SIZE,
        )
        self.SetMinSize(wx.Size(*theme.FRAME_MIN_SIZE))
        self.SetBackgroundColour(theme.BG)
        if ICON_PATH.exists():
            try:
                icon = wx.Icon(str(ICON_PATH), wx.BITMAP_TYPE_ICO)
                if icon.IsOk():
                    self.SetIcon(icon)
                else:
                    logger.warning("App icon at %s failed to load (not IsOk())", ICON_PATH)
            except Exception as e:
                logger.warning("Could not load app icon from %s (%s)", ICON_PATH, e)
        else:
            logger.warning("App icon not found at %s", ICON_PATH)
        self.settings = settings
        # GUI REWRITE: WebViewHost no longer owns a separate popup frame
        # (spec §6.1 inline embedding) -- attach() is called once
        # ReceiverHost/ReceiverLive exist, inside _build_widgets(); no
        # frame-visibility/close-routing setup needed here any more.
        self._webview_host = webview_host

        self.status_queue: "queue.Queue[GuiMessage]" = queue.Queue()
        self.rig_test_thread: Optional[threading.Thread] = None
        self.websdr_test_thread: Optional[threading.Thread] = None
        self.detect_thread: Optional[threading.Thread] = None

        # Set on a successful Detect click; the actual WebSDRSite to connect
        # to when the Custom URL sentinel is selected. None until detected.
        self._custom_site: Optional[WebSDRSite] = None

        # User-saved Custom URL sites (persisted via AppSettings.user_sites,
        # separate from the app's built-in KNOWN_SITES). Loaded before the
        # dropdown is built so it can include them from the start.
        self._user_sites: list[WebSDRSite] = [
            WebSDRSite(name=d["name"], url=d["url"], driver_type=d["driver_type"])
            for d in self.settings.user_sites
        ]

        # Sites loaded via the "Manage sites..." dialog (file/URL loads
        # share imported_sites; "Update from GitHub" populates
        # curated_sites) -- see _all_selectable_sites(). Deliberately kept
        # OUT of _find_any_site_by_url/_site_already_saved's scope (see
        # that method's docstring) -- only used for dropdown display and
        # name-lookup.
        self._imported_sites: list[WebSDRSite] = [
            WebSDRSite(name=d["name"], url=d["url"], driver_type=d["driver_type"])
            for d in self.settings.imported_sites
        ]
        self._curated_sites: list[WebSDRSite] = [
            WebSDRSite(name=d["name"], url=d["url"], driver_type=d["driver_type"])
            for d in self.settings.curated_sites
        ]

        # Mirrors of the engine's independent subsystem states, updated
        # from each StatusSnapshot -- drive button labels/enabled-state and
        # "connecting.../reconnecting..." status wording (see
        # _update_websdr_controls / _apply_snapshot).
        self._rig_active = False
        # Separate from _rig_active, which goes True the moment a rig
        # session is STARTED (client object created, connect deadline
        # armed) -- not once it's actually reachable. WebSDR Connect must
        # gate on this, the real "up and answering" state, or clicking
        # Connect while rigctld is still mid-handshake races a rig-connect
        # timeout that tears the whole engine down mid-WebView-creation.
        self._rig_connected = False
        self._rig_ever_connected = False
        self._websdr_active = False
        self._websdr_ever_connected = False
        # True from the moment Connect is clicked until the engine
        # either reports websdr_active=True (success) or an explicit
        # failure (snap.websdr is not None -- see _start_websdr's own
        # error-path publishes). SyncEngine._tick() keeps publishing
        # ordinary "nothing to report" snapshots (websdr_active=False,
        # websdr=None) on its regular cadence throughout the connect
        # attempt -- until _start_websdr() actually flips its internal
        # websdr_active True (only after the WebView round-trip completes,
        # a GUI-thread hop away), those ordinary snapshots are
        # indistinguishable on the wire from a genuine disconnect having
        # just completed (_stop_websdr() also publishes websdr=None).
        # This flag is what lets _apply_snapshot tell "still waiting, no
        # news yet" apart from "actually over" and skip tearing down the
        # connect-in-progress UI/window on a merely-stale snapshot.
        self._websdr_connect_pending = False
        # What the WebSDR panel last told the engine to load -- None means
        # "not active".
        self._active_websdr_site: Optional[WebSDRSite] = None
        # Mirrors the panel's current status text -- used the same way the
        # Tkinter version read the conn_var StringVar back for gating logic.
        self._websdr_conn_text = "not connected"
        # Dedicated gate for "Save to list" (can_save in
        # _update_websdr_controls), set in every branch of _apply_snapshot
        # (including the fatal-error branch) -- kept separate from
        # _websdr_conn_text, which is display text only.
        self._websdr_connected = False

        # Raw (pre-Wrap) text currently shown by each collapsing message
        # row -- see _update_message_row() for why the widget's own
        # GetLabel() can't be used for this.
        self._message_text: dict[wx.StaticText, str] = {}

        # GUI REWRITE IN PROGRESS: StatusSnapshot now dispatches to the
        # new _apply_status_snapshot() (drives AppState), NOT the old
        # ~150-line _apply_snapshot() below (still defined, still bound
        # to dead pre-rewrite widgets -- kept as reference for the phase
        # 10 cleanup, never called). The other three entries stay bound
        # to their original (also-currently-dead) handlers: nothing posts
        # RigPreflightResult/WebsdrPreflightResult/DetectResult yet
        # (Test/Detect are still deferred -- see settings_panels/), and
        # SiteListFetchResult only fires if _maybe_auto_update_curated_sites()
        # is re-enabled (it isn't).
        self._dispatch: dict[type, Callable[[GuiMessage], None]] = {
            StatusSnapshot: self._apply_status_snapshot,
            RigPreflightResult: self._apply_rig_preflight,
            WebsdrPreflightResult: self._apply_websdr_preflight,
            DetectResult: self._apply_detect_result,
            SiteListFetchResult: self._apply_curated_autofetch_result,
        }

        self.Bind(wx.EVT_CLOSE, self._on_close)

        # GUI REWRITE IN PROGRESS: one AppState instance, mutated directly
        # by the temporary click handlers below until phase 8 replaces
        # this with build_app_state() driven by real StatusSnapshots. See
        # sdrsync/gui/state.py.
        self._state = AppState(
            mute_on_tx=self.settings.mute_on_tx,
            mock_rig=self.settings.use_mock_rig,
            sync_tx_vfo=self.settings.sync_tx_vfo,
        )
        # Optimistic "Connecting.../Disconnecting.../Loading..." override
        # for StripPanel's connect button while a WebSDR connect/switch/
        # disconnect is in flight -- see _refresh_chrome(). None means
        # "derive the label from self._state.sdr_connected as normal".
        self._strip_connect_busy_label: Optional[str] = None

        self._build_widgets()
        self._restore_main_window_geometry()
        # GUI REWRITE IN PROGRESS: _restore_custom_site_if_needed() still
        # references self.site_combo/self.custom_url_entry/
        # self.detect_result_text, which the new StripPanel/SettingsHost
        # skeleton doesn't build (Custom URL entry lives in SitesPanel's
        # Add-a-site form, not wired to Detect yet -- see sites_panel.py).
        # Deferred to a later phase alongside Detect. Real, non-Custom-URL
        # sites (the overwhelming common case) are unaffected: their
        # names resolve fine via _find_selectable_site_by_url() inside
        # _wire_strip_panel() above.
        self._maybe_auto_update_curated_sites()

        # One engine, one background thread, for the whole app session --
        # NOT recreated per Connect click. The rig/WebSDR subsystems it
        # owns are started/stopped independently via the buttons below.
        self.engine = SyncEngine(self.settings, self.status_queue, webview_host=self._webview_host)
        self.thread = threading.Thread(target=self._run_engine, args=(self.engine,), daemon=True)
        self.thread.start()

        self._poll_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._poll_status_queue, self._poll_timer)
        self._poll_timer.Start(QUEUE_POLL_MS)

    def _restore_main_window_geometry(self) -> None:
        """Applies this window's own remembered POSITION from the last
        session, if any -- same idea as the WebSDR popup's geometry
        memory (_restore_webview_geometry). Falls back to wx's own
        default placement both when nothing was ever saved and when the
        saved point no longer lands on any connected display (e.g. a
        docking station's monitor unplugged since it was saved) -- the
        same has_display_at() check the WebSDR popup's geometry restore
        uses.

        Size is deliberately handled separately, in _finish_initial_layout()
        (queued via wx.CallAfter in __init__) rather than here: it needs
        this window's position already settled (to know which display to
        clamp against) and needs the frame to have actually been shown at
        least once for accurate metrics -- neither is true yet at this
        point in __init__."""
        pos = self.settings.main_window_position
        if pos and len(pos) == 2:
            candidate = wx.Point(pos[0], pos[1])
            if has_display_at(candidate):
                self.SetPosition(candidate)

    def _finish_initial_layout(self) -> None:
        """Queued via wx.CallAfter in __init__, so this runs right after
        SDRSyncApp.OnInit()'s frame.Show(True) -- best-size/virtual-size
        metrics are only accurate once this frame is genuinely realized
        on screen (confirmed live: _root_panel's computed best size grew
        by dozens of pixels between construction and the first real
        Show()+Layout() pass -- enough that "Open log folder", the last
        always-present widget, landed just past the frame's own
        bottom edge before this ran).

        Establishes a hard floor -- this window's own MinSize -- from the
        natural content size with the Mock Rig Control panel HIDDEN (its
        baseline, always-available state): whatever else happens (a user
        drag-resize, a future change here), this window can never shrink
        past the point where "Open log folder" would go out of view.
        Then hands off to _resize_main_window_to_content() for the actual
        sizing, which _update_mock_rig_panel_visibility() also calls on
        every rig/mock-rig connect and disconnect, so the window grows
        and shrinks live with whatever's currently visible rather than
        only ever growing (Fit()'s old behavior)."""
        self._root_panel.Layout()
        self._root_panel.FitInside()
        # mock_box is still in its construction-time hidden state here --
        # the Mock Rig Control panel only ever appears after a rig
        # connects, which requires the GUI to already be interactive, so
        # it can't have happened yet this early in startup. That's what
        # makes this the right moment to capture the MinSize floor below.
        # GetSizer().CalcMin(), not _root_panel.GetBestSize() -- a
        # wx.ScrolledWindow's own best-size doesn't reliably reflect its
        # sizer's true minimum the way a plain Panel's does (confirmed
        # live: it under-reported the natural content height, exactly
        # the "content unreachable" failure mode this whole mechanism
        # exists to prevent).
        min_natural = self._root_panel.GetSizer().CalcMin()
        chrome = self.GetSize() - self.GetClientSize()
        self.SetMinSize(wx.Size(min_natural.width + chrome.width, min_natural.height + chrome.height))
        self._resize_main_window_to_content()

    def _resize_main_window_to_content(self) -> None:
        """Resizes this window's own OUTER size to exactly fit whatever's
        CURRENTLY visible (the Mock Rig Control panel included or
        excluded), clamped to whichever display it's on. Called once at
        startup (via _finish_initial_layout()) and again every time
        _update_mock_rig_panel_visibility() toggles that panel.

        SetMinSize() (see _finish_initial_layout()) guarantees the
        SetSize() below can never actually shrink this window past the
        point "Open log folder" would go out of view. FitInside() keeps
        _root_panel's scrollbar range in sync for whatever doesn't fit
        even at the clamped size -- content taller than the display is
        still fully reachable, just via scrolling rather than a window
        that would otherwise be taller than the screen itself."""
        self._root_panel.Layout()
        self._root_panel.FitInside()
        natural = self._root_panel.GetSizer().CalcMin()
        # clamp_size_to_display bounds an OUTER top-level window size
        # (see its other use, on the WebSDR popup's frame.SetSize()) --
        # natural is a CLIENT size (what _root_panel needs), so convert
        # through this frame's own chrome (title bar/borders) in both
        # directions, or the clamp bound (and the size we end up
        # requesting) is off by however tall the title bar is.
        chrome = self.GetSize() - self.GetClientSize()
        outer_target = clamp_size_to_display(
            wx.Size(natural.width + chrome.width, natural.height + chrome.height), self.GetPosition(),
        )
        self.SetSize(outer_target)

    @staticmethod
    def _run_engine(engine: SyncEngine) -> None:
        try:
            asyncio.run(engine.run())
        except Exception as e:
            logger.exception("Sync engine crashed")
            engine.publish_fatal_error(_describe_engine_crash(e))

    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        """spec §2's five-band root layout. GUI REWRITE IN PROGRESS: each
        band is a _PlaceholderBand until its own phase replaces it with
        the real module (StripPanel/SectionBar/SettingsHost/ReceiverHost/
        StatusBarPanel) -- see project_brief.md's progress log for which
        phase is current. The old _build_websdr_panel/_build_rig_panel/
        _build_mock_rig_panel methods below are temporarily unused (kept
        for their business logic until it's ported into the new settings
        panels in phase 5; deleted in the phase 10 cleanup pass)."""
        root = wx.BoxSizer(wx.VERTICAL)

        self.strip_panel = StripPanel(self)
        root.Add(self.strip_panel, 0, wx.EXPAND)
        self._wire_strip_panel()

        self.section_bar = SectionBar(self)
        root.Add(self.section_bar, 0, wx.EXPAND)
        self._wire_section_bar()

        self.settings_host = SettingsHost(self)
        root.Add(self.settings_host, 0, wx.EXPAND)
        self._build_settings_panels()

        self.receiver_host = ReceiverHost(self)
        root.Add(self.receiver_host, 1, wx.EXPAND)
        self._webview_host.attach(self.receiver_host.live.webview_parent)
        self.receiver_host.idle.on_primary = self._on_idle_primary_clicked
        self.receiver_host.idle.on_secondary = lambda: self._open_settings_panel("transceiver")

        self.status_bar_panel = _PlaceholderBand(
            self, "StatusBarPanel", height=theme.STATUS_BAR_HEIGHT, fill=theme.PANEL_ALT, hairline_edge="top",
        )
        root.Add(self.status_bar_panel, 0, wx.EXPAND)

        self.SetSizer(root)
        self._refresh_chrome()

    def _refresh_chrome(self) -> None:
        """Pushes self._state to every chrome band that's real so far
        (StatusBarPanel joins once its own phase lands). One call after
        any state mutation -- matches the "no-op tick costs nothing"
        discipline each refresh(state) individually keeps."""
        self.strip_panel.refresh(self._state)
        if self._strip_connect_busy_label is not None:
            # A click-site optimistic override while a connect/disconnect
            # is in flight -- StripPanel.refresh() derives the label from
            # state.sdr_connected alone, which hasn't flipped yet, so it
            # would otherwise flicker back to "Connect"/"Disconnect"
            # before the real StatusSnapshot resolves. Cleared by
            # _apply_snapshot() once it does (mirrors the pre-rewrite
            # _websdr_connect_pending gate).
            self.strip_panel.connect_btn.SetLabel(self._strip_connect_busy_label)
            self.strip_panel.connect_btn.Enable(False)
        else:
            self.strip_panel.connect_btn.Enable(self._state.rig_connected or self._state.sdr_connected)
        self.section_bar.refresh(self._state)
        self.receiver_host.refresh(self._state)

    def _wire_strip_panel(self) -> None:
        sp = self.strip_panel
        sp.pause_btn.Bind(wx.EVT_TOGGLEBUTTON, self._on_pause_toggled)
        sp.mute_btn.Bind(wx.EVT_TOGGLEBUTTON, self._on_strip_mute_toggled)
        sp.connect_btn.Bind(wx.EVT_BUTTON, self._on_strip_connect_clicked)
        # GUI REWRITE IN PROGRESS: populated from KNOWN_SITES + user_sites
        # (no background thread needed for either) -- curated/imported
        # sites reach this combo only via Sites panel's Load (see
        # _on_sites_panel_load), which adds the clicked entry if it's not
        # already here.
        sp.site_choice.Set([s.name for s in KNOWN_SITES + self._user_sites])
        selected = self._find_selectable_site_by_url(self.settings.last_site_url)
        sp.site_choice.SetStringSelection(selected.name if selected else KNOWN_SITES[0].name)

    def _wire_section_bar(self) -> None:
        for key, btn in self.section_bar.panel_buttons.items():
            btn.Bind(wx.EVT_TOGGLEBUTTON, lambda evt, k=key: self._on_section_panel_toggled(k))

    def _on_pause_toggled(self, _evt: wx.CommandEvent) -> None:
        # GUI REWRITE IN PROGRESS: flips the display flag only -- phase 8
        # replaces this with a real call to
        # engine.set_forward_sync_paused_from_other_thread(), reading the
        # displayed value back from the next StatusSnapshot rather than
        # setting it optimistically here.
        self._state.paused = not self._state.paused
        self._refresh_chrome()

    def _on_strip_mute_toggled(self, _evt: wx.CommandEvent) -> None:
        self._state.mute_on_tx = not self._state.mute_on_tx
        self.settings.mute_on_tx = self._state.mute_on_tx
        self.settings.save()
        self._refresh_chrome()

    def _resolve_strip_selected_site(self) -> Optional[WebSDRSite]:
        name = self.strip_panel.site_choice.GetStringSelection()
        return next((s for s in self._all_selectable_sites() if s.name == name), None)

    def _on_strip_connect_clicked(self, _evt: wx.CommandEvent = None) -> None:
        if self.engine is None:
            return
        if self._websdr_active:
            active = self._active_websdr_site
            site = self._resolve_strip_selected_site()
            if site is not None and active is not None and site.url == active.url:
                self._disconnect_websdr()
                return
            if site is None:
                return
            self._begin_websdr_switch(site)
            return
        if not self._state.rig_connected:
            return  # belt-and-suspenders -- the button is disabled in this state
        site = self._resolve_strip_selected_site()
        if site is None:
            return
        self._begin_websdr_connect(site)

    def _begin_websdr_switch(self, site: WebSDRSite) -> None:
        self.settings.last_site_url = site.url
        self.settings.last_site_driver_type = site.driver_type if self._find_selectable_site_by_url(site.url) is None else ""
        self.settings.save()
        self._active_websdr_site = site
        self._strip_connect_busy_label = "Loading..."
        self._refresh_chrome()
        self.engine.switch_websdr_from_other_thread(site)

    def _begin_websdr_connect(self, site: WebSDRSite) -> None:
        self._websdr_connect_pending = True
        self.settings.last_site_url = site.url
        self.settings.last_site_driver_type = site.driver_type if self._find_selectable_site_by_url(site.url) is None else ""
        self.settings.save()
        self._websdr_ever_connected = False
        self._active_websdr_site = site
        self._strip_connect_busy_label = "Connecting..."
        self._state.sdr_active = True  # ReceiverHost must show Live now -- see AppState.sdr_active
        self._refresh_chrome()
        bring_pair_to_front(self)
        self.engine.start_websdr_from_other_thread(site)

    def _disconnect_websdr(self) -> None:
        if not self._websdr_active:
            return
        self._websdr_connect_pending = False
        self._strip_connect_busy_label = "Disconnecting..."
        self._refresh_chrome()
        self.engine.stop_websdr_from_other_thread()

    def _on_idle_primary_clicked(self) -> None:
        if not self._state.rig_connected:
            self._on_transceiver_connect_clicked()
        else:
            self._open_settings_panel("sites")

    def _open_settings_panel(self, key: Optional[str]) -> None:
        self._state.open_panel = key
        self.settings_host.show_panel(key)
        self._refresh_chrome()

    def _on_section_panel_toggled(self, key: str) -> None:
        self._open_settings_panel(None if self._state.open_panel == key else key)

    def _build_settings_panels(self) -> None:
        self.transceiver_panel = TransceiverPanel(self.settings_host, self.settings)
        self.transceiver_panel.on_connect = self._on_transceiver_connect_clicked
        self.transceiver_panel.mock_panel.on_set_freq = self._on_mock_set_freq
        self.transceiver_panel.mock_panel.on_set_mode = self._on_mock_set_mode
        self.transceiver_panel.mock_panel.on_ptt_toggled = self._on_mock_ptt_toggled
        self.settings_host.add_panel("transceiver", self.transceiver_panel)

        self.sites_panel = SitesPanel(self.settings_host, self.settings)
        self.sites_panel.on_load_site = self._on_sites_panel_load
        self.settings_host.add_panel("sites", self.sites_panel)

        self.behaviour_panel = BehaviourPanel(self.settings_host, self.settings)
        self.behaviour_panel.on_sync_tx_vfo_changed = self._on_sync_tx_vfo_changed
        self.behaviour_panel.on_sync_direction_changed = self._on_sync_direction_changed
        self.settings_host.add_panel("behaviour", self.behaviour_panel)

    def _on_sync_tx_vfo_changed(self, value: bool) -> None:
        self._state.sync_tx_vfo = value
        self._refresh_chrome()

    def _on_sync_direction_changed(self, held: bool) -> None:
        if self.engine is not None:
            self.engine.set_reverse_sync_held(held)

    def _on_sites_panel_load(self, site: WebSDRSite) -> None:
        names = list(self.strip_panel.site_choice.GetStrings())
        if site.name not in names:
            names.append(site.name)
            self.strip_panel.site_choice.Set(names)
        self.strip_panel.site_choice.SetStringSelection(site.name)
        self._open_settings_panel(None)
        if self._websdr_active:
            self._begin_websdr_switch(site)
        elif self._state.rig_connected:
            self._begin_websdr_connect(site)

    # ------------------------------------------------------------------ Transceiver (real, phase 6)
    def _on_transceiver_connect_clicked(self) -> None:
        if self.engine is None:
            return
        if self._rig_active:
            self.transceiver_panel.set_connection_state(True, busy_label="Disconnecting...")
            self.engine.stop_rig_from_other_thread()
            return
        tp = self.transceiver_panel
        backend = tp.backend_choice.GetStringSelection()
        use_mock = tp.mock_rig_check.GetValue()
        # A mock rig only ever exists on loopback -- ignore whatever's
        # typed in the host field rather than silently trying to bind a
        # mock server to some other address.
        host = "127.0.0.1" if use_mock else (tp.host_entry.GetValue().strip() or "127.0.0.1")
        port = tp.port_entry.GetValue()
        if backend == "flrig":
            self.settings.flrig_host, self.settings.flrig_port = host, port
        else:
            self.settings.rigctld_host, self.settings.rigctld_port = host, port
        self.settings.rig_backend = backend
        self.settings.use_mock_rig = use_mock
        self.settings.save()
        self._state.mock_rig = use_mock
        self._rig_ever_connected = False
        tp.set_connection_state(False, busy_label="Connecting...")
        self.engine.start_rig_from_other_thread(backend, host, port, use_mock)

    def _on_mock_set_freq(self, text: str) -> None:
        if self.engine is None:
            return
        try:
            self.engine.push_mock_freq(int(text.strip()))
        except ValueError:
            pass

    def _on_mock_set_mode(self, mode: str, passband_text: str) -> None:
        if self.engine is None:
            return
        try:
            passband = int(passband_text.strip())
        except ValueError:
            passband = 2400
        self.engine.push_mock_mode(mode, passband)

    def _on_mock_ptt_toggled(self, is_tx: bool) -> None:
        if self.engine is not None:
            self.engine.push_mock_ptt(is_tx)

    def _build_websdr_panel(self, parent: wx.Window, outer: wx.BoxSizer) -> None:
        grid = wx.GridBagSizer(vgap=2, hgap=6)
        row = 0

        grid.Add(wx.StaticText(parent, label="WebSDR site:"), pos=(row, 0), flag=wx.ALIGN_CENTER_VERTICAL)
        selected_site = self._find_selectable_site_by_url(self.settings.last_site_url) or KNOWN_SITES[0]
        # No value= here: a CB_READONLY combo silently discards it while
        # choices=[] (there's nothing yet for it to match), so GetValue()
        # reads back "" until _refresh_site_dropdown_values() below
        # actually populates the list -- confirmed live: that "" then
        # failed _refresh_site_dropdown_values()'s "restore current
        # selection" check and silently fell back to KNOWN_SITES[0]
        # (Twente) on every single restart, regardless of the real
        # last-used site. Set explicitly after populating instead.
        self.site_combo = wx.ComboBox(parent, choices=[], style=wx.CB_READONLY)
        self.site_combo.Bind(wx.EVT_COMBOBOX, self._on_site_selected)
        grid.Add(self.site_combo, pos=(row, 1), span=(1, 2), flag=wx.EXPAND)

        self.websdr_connect_btn = wx.Button(parent, label="Connect")
        self.websdr_connect_btn.Bind(wx.EVT_BUTTON, self._on_websdr_connect_clicked)
        _Tooltip(self.websdr_connect_btn, self._websdr_connect_tooltip_text)
        # One toggle button for the whole Connect/Disconnect lifecycle, same
        # pattern as the Transceiver panel's rig_connect_btn -- relabeled
        # in place rather than a separate always-there Disconnect button.
        # Reserve room for the widest label it's ever relabeled to at
        # runtime ("Disconnecting...", see _on_websdr_connect_clicked) --
        # the GridBagSizer column's width is computed once, at Fit() time
        # in _build_widgets, from whatever label is showing then
        # ("Connect"); relabeling later doesn't retrigger that sizing, so
        # the wider text was getting clipped. A few px of margin on top of
        # the exact text width so it doesn't look pixel-tight either.
        widest_label = "Disconnecting..."
        self.websdr_connect_btn.SetLabel(widest_label)
        min_size = self.websdr_connect_btn.GetBestSize()
        self.websdr_connect_btn.SetMinSize(wx.Size(min_size.width + 12, min_size.height))
        self.websdr_connect_btn.SetLabel("Connect")
        grid.Add(self.websdr_connect_btn, pos=(row, 3), flag=wx.EXPAND)

        self.delete_site_btn = wx.Button(parent, label="Delete")
        self.delete_site_btn.Bind(wx.EVT_BUTTON, self._on_delete_site_clicked)
        _Tooltip(self.delete_site_btn, self._delete_site_tooltip_text)
        grid.Add(self.delete_site_btn, pos=(row, 4), flag=wx.EXPAND)
        self._refresh_site_dropdown_values()
        self.site_combo.SetValue(selected_site.name)
        row += 1

        manage_sites_btn = wx.Button(parent, label="Manage sites...")
        manage_sites_btn.Bind(wx.EVT_BUTTON, self._on_manage_sites_clicked)
        grid.Add(manage_sites_btn, pos=(row, 0), flag=wx.ALIGN_LEFT)

        self.reverse_sync_hold_btn = wx.CheckBox(parent, label="Hold (WebSDR read-only)")
        self.reverse_sync_hold_btn.Bind(wx.EVT_CHECKBOX, self._on_reverse_sync_hold_toggled)
        _Tooltip(
            self.reverse_sync_hold_btn,
            lambda: "Pause the WebSDR page from moving your rig. Forward sync (rig -> WebSDR) "
                    "keeps working. Can be toggled before connecting. A write already on its "
                    "way to the rig may still complete once -- it can't be recalled mid-send.",
        )
        grid.Add(self.reverse_sync_hold_btn, pos=(row, 2), flag=wx.ALIGN_LEFT)
        self.reverse_sync_held_text = wx.StaticText(parent, label="")
        self.reverse_sync_held_text.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        grid.Add(self.reverse_sync_held_text, pos=(row, 3), span=(1, 2), flag=wx.ALIGN_CENTER_VERTICAL)
        row += 1

        grid.Add(wx.StaticText(parent, label="Custom URL:"), pos=(row, 0), flag=wx.ALIGN_CENTER_VERTICAL)
        self.custom_url_entry = wx.TextCtrl(parent, value="")
        self.custom_url_entry.Bind(wx.EVT_TEXT, self._on_custom_url_edited)
        grid.Add(self.custom_url_entry, pos=(row, 1), span=(1, 2), flag=wx.EXPAND)
        self.detect_btn = wx.Button(parent, label="Detect")
        self.detect_btn.Bind(wx.EVT_BUTTON, self._on_detect_clicked)
        grid.Add(self.detect_btn, pos=(row, 3), flag=wx.EXPAND)
        row += 1

        self.detect_result_text = wx.StaticText(parent, label="", size=(300, -1))
        grid.Add(self.detect_result_text, pos=(row, 0), span=(1, 3), flag=wx.EXPAND)
        self.save_site_btn = wx.Button(parent, label="Save to list")
        self.save_site_btn.Bind(wx.EVT_BUTTON, self._on_save_site_clicked)
        _Tooltip(self.save_site_btn, self._save_site_tooltip_text)
        grid.Add(self.save_site_btn, pos=(row, 3), flag=wx.EXPAND)
        row += 1

        self.headless_check = wx.CheckBox(parent, label="Hide browser window (audio still plays)")
        self.headless_check.SetValue(self.settings.headless)
        grid.Add(self.headless_check, pos=(row, 0), span=(1, 3), flag=wx.ALIGN_CENTER_VERTICAL)
        self.websdr_test_btn = wx.Button(parent, label="Test")
        self.websdr_test_btn.Bind(wx.EVT_BUTTON, self._on_websdr_test_clicked)
        grid.Add(self.websdr_test_btn, pos=(row, 3), flag=wx.EXPAND)
        row += 1

        grid.Add(wx.StaticText(parent, label="CW offset (Hz):"), pos=(row, 0), flag=wx.ALIGN_CENTER_VERTICAL)
        self.cw_offset_ctrl = wx.SpinCtrl(parent, min=-2000, max=2000, initial=self.settings.cw_offset_hz, size=(80, -1))
        self.cw_offset_ctrl.Bind(wx.EVT_SPINCTRL, self._on_cw_offset_changed)
        _Tooltip(self.cw_offset_ctrl, lambda: "Disconnect and reconnect to apply a new CW offset.")
        grid.Add(self.cw_offset_ctrl, pos=(row, 1), flag=wx.ALIGN_CENTER_VERTICAL)
        self.mute_on_tx_check = wx.CheckBox(parent, label="Mute WebSDR on TX")
        self.mute_on_tx_check.SetValue(self.settings.mute_on_tx)
        _Tooltip(self.mute_on_tx_check, lambda: "Takes effect on the next PTT transition, not instantly.")
        self.mute_on_tx_check.Bind(wx.EVT_CHECKBOX, self._on_mute_on_tx_changed)
        grid.Add(self.mute_on_tx_check, pos=(row, 2), span=(1, 2), flag=wx.ALIGN_CENTER_VERTICAL)
        row += 1

        grid.Add(wx.StaticText(parent, label="Idle disconnect (min):"), pos=(row, 0),
                 flag=wx.ALIGN_CENTER_VERTICAL)
        self.idle_disconnect_entry = wx.TextCtrl(
            parent,
            value=(str(self.settings.websdr_idle_disconnect_min)
                   if self.settings.websdr_idle_disconnect_min is not None else ""),
            size=(90, -1),
            style=wx.TE_PROCESS_ENTER,
        )
        # Commits on Enter/blur only, never per keystroke -- same pattern
        # as the reverse-sync range fields (see
        # _on_idle_disconnect_commit), so a field momentarily blank while
        # retyping can't be persisted as "disabled".
        self.idle_disconnect_entry.Bind(wx.EVT_TEXT_ENTER, self._on_idle_disconnect_commit)
        self.idle_disconnect_entry.Bind(wx.EVT_KILL_FOCUS, self._on_idle_disconnect_commit)
        _Tooltip(
            self.idle_disconnect_entry,
            lambda: "Release the WebSDR audio slot after this many minutes with no rig activity "
                    "(no frequency, mode or PTT change). Reconnects automatically as soon as you "
                    "use the rig again. Blank or 0 = never disconnect. Public receivers are "
                    "volunteer-run and have limited slots. Applies when you press Enter or leave "
                    "the field.",
        )
        grid.Add(self.idle_disconnect_entry, pos=(row, 1), flag=wx.ALIGN_CENTER_VERTICAL)
        self.websdr_idle_text = wx.StaticText(parent, label="")
        self.websdr_idle_text.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        grid.Add(self.websdr_idle_text, pos=(row, 2), span=(1, 2), flag=wx.ALIGN_CENTER_VERTICAL)
        row += 1

        # --- Live status readout ---------------------------------------
        # Paired two-across (see _grid_status_pair): five one-per-row
        # readouts cost five rows of a panel that already had columns 2-3
        # sitting empty beside them. Pairing is by kind -- what this
        # connection IS (state + which driver is driving it), then what
        # it's DOING (frequency + mode), then whether audio is actually
        # flowing.
        self.websdr_conn_text = wx.StaticText(parent, label="not connected")
        self.websdr_driver_text = wx.StaticText(parent, label="-")
        self.websdr_freq_text = wx.StaticText(parent, label="-")
        self.websdr_mode_text = wx.StaticText(parent, label="-")
        self.websdr_audio_text = wx.StaticText(parent, label="-")
        row = self._grid_status_pair(parent, grid, row,
                                     "Status:", self.websdr_conn_text, "Driver:", self.websdr_driver_text)
        row = self._grid_status_pair(parent, grid, row,
                                     "Frequency:", self.websdr_freq_text, "Mode:", self.websdr_mode_text)
        row = self._grid_status_row(parent, grid, row, "Audio:", self.websdr_audio_text)

        # --- Message area (collapsing -- see _add_message_row) ---------
        # Every message this panel can raise, gathered in one place under
        # the status block instead of scattered between the controls, in
        # descending severity: hard failure, reverse-sync gave up,
        # reverse-sync still trying, Test result. Each line appears only
        # while it has something to say, so the normal (quiet) case costs
        # nothing at all -- and because they sit BELOW the readout, a
        # message appearing no longer shoves the status block down the
        # panel while the user is reading it.
        self.websdr_err_text, row = self._add_message_row(parent, grid, row, ERROR_COLOUR)
        # Final/actionable (red -- the retry ladder gave up) vs
        # transient/informational (grey -- a retry is still in progress).
        # Two independent rows, NOT one shared line: the engine's
        # reverse_sync_error persists until a later push succeeds (or the
        # WebSDR reattaches), so a stale freq give-up and a live mode
        # retry genuinely can be pending at the same moment
        # (SyncEngine._reverse_sync_error vs _reverse_sync_pending_text()
        # are separately maintained). Sharing one row would silently drop
        # whichever lost.
        self.reverse_sync_error_text, row = self._add_message_row(parent, grid, row, ERROR_COLOUR)
        self.reverse_sync_pending_text, row = self._add_message_row(
            parent, grid, row, wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT),
        )
        self.websdr_preflight_text, row = self._add_message_row(parent, grid, row)

        grid.AddGrowableCol(1)
        outer.Add(grid, flag=wx.EXPAND | wx.ALL, border=2)
        self._update_websdr_controls()

    def _build_rig_panel(self, parent: wx.Window, outer: wx.BoxSizer) -> None:
        grid = wx.GridBagSizer(vgap=2, hgap=6)
        row = 0

        grid.Add(wx.StaticText(parent, label="Backend:"), pos=(row, 0), flag=wx.ALIGN_CENTER_VERTICAL)
        self.rig_backend_combo = wx.ComboBox(
            parent, value=self.settings.rig_backend, choices=RIG_BACKEND_CHOICES, style=wx.CB_READONLY,
        )
        self.rig_backend_combo.Bind(wx.EVT_COMBOBOX, self._on_rig_backend_selected)
        grid.Add(self.rig_backend_combo, pos=(row, 1), span=(1, 3), flag=wx.EXPAND)
        row += 1

        grid.Add(wx.StaticText(parent, label="host:"), pos=(row, 0), flag=wx.ALIGN_CENTER_VERTICAL)
        self.host_entry = wx.TextCtrl(parent, size=(140, -1))
        grid.Add(self.host_entry, pos=(row, 1), flag=wx.ALIGN_CENTER_VERTICAL)
        grid.Add(wx.StaticText(parent, label="port:"), pos=(row, 2),
                 flag=wx.ALIGN_CENTER_VERTICAL | wx.ALIGN_RIGHT)
        self.port_entry = wx.TextCtrl(parent, size=(80, -1))
        grid.Add(self.port_entry, pos=(row, 3), flag=wx.ALIGN_CENTER_VERTICAL)
        self._populate_host_port_for_backend(self.settings.rig_backend)
        row += 1

        grid.Add(wx.StaticText(parent, label="Poll interval (s):"), pos=(row, 0),
                 flag=wx.ALIGN_CENTER_VERTICAL)
        self.poll_interval_ctrl = wx.SpinCtrlDouble(
            parent, min=MIN_POLL_INTERVAL_S, max=MAX_POLL_INTERVAL_S, inc=0.05,
            initial=self.settings.poll_interval_s, size=(80, -1),
        )
        self.poll_interval_ctrl.SetDigits(2)
        self.poll_interval_ctrl.Bind(wx.EVT_SPINCTRLDOUBLE, self._on_poll_interval_changed)
        grid.Add(self.poll_interval_ctrl, pos=(row, 1), flag=wx.ALIGN_CENTER_VERTICAL)
        row += 1

        grid.Add(wx.StaticText(parent, label="Reverse-sync range (Hz):"), pos=(row, 0), flag=wx.ALIGN_CENTER_VERTICAL)
        self.reverse_sync_min_entry = wx.TextCtrl(
            parent,
            value=str(self.settings.reverse_sync_min_hz) if self.settings.reverse_sync_min_hz is not None else "",
            size=(90, -1),
            style=wx.TE_PROCESS_ENTER,
        )
        _Tooltip(
            self.reverse_sync_min_entry,
            lambda: "Lowest Hz reverse sync (WebSDR -> rig) may write to your rig. Blank = no lower bound. "
                    "Applies when you press Enter or leave the field.",
        )
        grid.Add(self.reverse_sync_min_entry, pos=(row, 1), flag=wx.ALIGN_CENTER_VERTICAL)
        grid.Add(wx.StaticText(parent, label="to:"), pos=(row, 2), flag=wx.ALIGN_CENTER_VERTICAL | wx.ALIGN_RIGHT)
        self.reverse_sync_max_entry = wx.TextCtrl(
            parent,
            value=str(self.settings.reverse_sync_max_hz) if self.settings.reverse_sync_max_hz is not None else "",
            size=(90, -1),
            style=wx.TE_PROCESS_ENTER,
        )
        # Both fields commit on Enter/blur only, never per keystroke (see
        # _on_reverse_sync_range_commit). Bound only now that BOTH
        # controls exist -- the handler reads them as a pair, so binding
        # the min field earlier would leave a window where a kill-focus
        # event (wx can move focus as later controls are created) reaches
        # a handler whose max field isn't assigned yet.
        for entry in (self.reverse_sync_min_entry, self.reverse_sync_max_entry):
            entry.Bind(wx.EVT_TEXT_ENTER, self._on_reverse_sync_range_commit)
            entry.Bind(wx.EVT_KILL_FOCUS, self._on_reverse_sync_range_commit)
        _Tooltip(
            self.reverse_sync_max_entry,
            lambda: "Highest Hz reverse sync (WebSDR -> rig) may write to your rig. Blank = no upper bound. "
                    "Applies when you press Enter or leave the field.",
        )
        grid.Add(self.reverse_sync_max_entry, pos=(row, 3), flag=wx.ALIGN_CENTER_VERTICAL)
        row += 1

        self.mock_rig_check = wx.CheckBox(parent, label="Use mock rig (embedded, for testing)")
        self.mock_rig_check.SetValue(self.settings.use_mock_rig)
        self.mock_rig_check.Bind(wx.EVT_CHECKBOX, self._on_mock_rig_toggled)
        grid.Add(self.mock_rig_check, pos=(row, 0), span=(1, 4), flag=wx.ALIGN_CENTER_VERTICAL)
        row += 1

        self.rig_connect_btn = wx.Button(parent, label="Connect")
        self.rig_connect_btn.Bind(wx.EVT_BUTTON, self._on_rig_connect_clicked)
        grid.Add(self.rig_connect_btn, pos=(row, 0), span=(1, 3), flag=wx.EXPAND)
        self.rig_test_btn = wx.Button(parent, label="Test")
        self.rig_test_btn.Bind(wx.EVT_BUTTON, self._on_rig_test_clicked)
        grid.Add(self.rig_test_btn, pos=(row, 3), flag=wx.EXPAND)
        row += 1

        # --- Live status readout ---------------------------------------
        # Same two-across pairing as the WebSDR panel, same grouping
        # logic: connection state beside PTT (the two "what is the radio
        # doing right now" facts, and PTT is the one an operator glances
        # at mid-QSO), then frequency beside mode.
        self.rig_conn_text = wx.StaticText(parent, label="not connected")
        self.rig_freq_text = wx.StaticText(parent, label="-")
        self.rig_mode_text = wx.StaticText(parent, label="-")
        self.rig_ptt_text = wx.StaticText(parent, label="-")
        row = self._grid_status_pair(parent, grid, row,
                                     "Status:", self.rig_conn_text, "PTT:", self.rig_ptt_text)
        row = self._grid_status_pair(parent, grid, row,
                                     "Frequency:", self.rig_freq_text, "Mode:", self.rig_mode_text)

        # --- Message area (collapsing -- see _add_message_row) ---------
        # Error first, then the Test (preflight) result; both collapsed
        # away entirely while empty, which is nearly always.
        self.rig_err_text, row = self._add_message_row(parent, grid, row, ERROR_COLOUR)
        self.rig_preflight_text, row = self._add_message_row(parent, grid, row)

        grid.AddGrowableCol(1)
        outer.Add(grid, flag=wx.EXPAND | wx.ALL, border=2)

        if self.mock_rig_check.GetValue():
            self.host_entry.Disable()

    def _build_mock_rig_panel(self, parent: wx.Window, outer_sizer: wx.BoxSizer) -> None:
        """Only ever shown while 'Use mock rig' is checked AND the rig
        subsystem is connected -- a real-rig session must never show
        controls that look like they drive a real radio."""
        self.mock_box = wx.StaticBox(parent, label="Mock Rig Control")
        mock_sizer = wx.StaticBoxSizer(self.mock_box, wx.VERTICAL)
        grid = wx.GridBagSizer(vgap=2, hgap=6)

        self.mock_freq_entry = wx.TextCtrl(self.mock_box, value="14074000", size=(120, -1))
        grid.Add(wx.StaticText(self.mock_box, label="Freq (Hz):"), pos=(0, 0), flag=wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.mock_freq_entry, pos=(0, 1), flag=wx.ALIGN_CENTER_VERTICAL)
        set_freq_btn = wx.Button(self.mock_box, label="Set Freq")
        set_freq_btn.Bind(wx.EVT_BUTTON, self._on_mock_set_freq)
        grid.Add(set_freq_btn, pos=(0, 2), flag=wx.ALIGN_CENTER_VERTICAL)

        self.mock_mode_combo = wx.ComboBox(
            self.mock_box, value="USB", choices=["USB", "LSB", "CW", "AM", "FM"], style=wx.CB_READONLY,
        )
        grid.Add(wx.StaticText(self.mock_box, label="Mode:"), pos=(1, 0), flag=wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.mock_mode_combo, pos=(1, 1), flag=wx.ALIGN_CENTER_VERTICAL)

        self.mock_passband_entry = wx.TextCtrl(self.mock_box, value="2400", size=(120, -1))
        grid.Add(wx.StaticText(self.mock_box, label="Passband (Hz):"), pos=(2, 0), flag=wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.mock_passband_entry, pos=(2, 1), flag=wx.ALIGN_CENTER_VERTICAL)
        set_mode_btn = wx.Button(self.mock_box, label="Set Mode")
        set_mode_btn.Bind(wx.EVT_BUTTON, self._on_mock_set_mode)
        grid.Add(set_mode_btn, pos=(2, 2), flag=wx.ALIGN_CENTER_VERTICAL)

        self.mock_ptt_check = wx.CheckBox(self.mock_box, label="PTT (transmitting)")
        self.mock_ptt_check.Bind(wx.EVT_CHECKBOX, self._on_mock_ptt_toggled)
        grid.Add(self.mock_ptt_check, pos=(3, 0), span=(1, 2), flag=wx.ALIGN_CENTER_VERTICAL)

        mock_sizer.Add(grid, flag=wx.EXPAND | wx.ALL, border=2)
        self.mock_err_text = self._new_message_text(self.mock_box, ERROR_COLOUR)
        mock_sizer.Add(self.mock_err_text, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=2)

        outer_sizer.Add(mock_sizer, flag=wx.EXPAND | wx.ALL, border=3)
        self.mock_box.Show(False)  # hidden until mock mode + rig connected

    @staticmethod
    def _grid_status_row(parent: wx.Window, grid: wx.GridBagSizer, row: int, label: str, value_widget: wx.StaticText) -> int:
        grid.Add(wx.StaticText(parent, label=label), pos=(row, 0), flag=wx.ALIGN_CENTER_VERTICAL)
        grid.Add(value_widget, pos=(row, 1), flag=wx.ALIGN_CENTER_VERTICAL)
        return row + 1

    @staticmethod
    def _grid_status_pair(
        parent: wx.Window, grid: wx.GridBagSizer, row: int,
        label_a: str, widget_a: wx.StaticText, label_b: str, widget_b: wx.StaticText,
    ) -> int:
        """Two label/value readouts on ONE grid row: the first in the
        col 0/1 pair a plain _grid_status_row() would use, the second in
        the col 2/3 pair (label right-aligned against its value, the same
        convention the Transceiver panel's 'port:'/'to:' labels already
        use). Halves the vertical space a status block costs without
        making it any denser to read -- these are short, fixed-width
        key/value pairs, and columns 2-3 were otherwise entirely blank
        alongside them.

        Both values still land in columns whose minimum width is already
        set by much wider content above them (the site combo and the
        Connect/Disconnect buttons here, the host/port fields in the
        Transceiver panel), so a value changing at runtime -- a longer
        frequency string, a longer driver name -- can't push this
        window's width around."""
        grid.Add(wx.StaticText(parent, label=label_a), pos=(row, 0), flag=wx.ALIGN_CENTER_VERTICAL)
        grid.Add(widget_a, pos=(row, 1), flag=wx.ALIGN_CENTER_VERTICAL)
        grid.Add(wx.StaticText(parent, label=label_b), pos=(row, 2),
                 flag=wx.ALIGN_CENTER_VERTICAL | wx.ALIGN_RIGHT)
        grid.Add(widget_b, pos=(row, 3), flag=wx.ALIGN_CENTER_VERTICAL)
        return row + 1

    def _add_message_row(
        self, parent: wx.Window, grid: wx.GridBagSizer, row: int, colour: Optional[wx.Colour] = None,
    ) -> tuple[wx.StaticText, int]:
        """One line of a panel's collapsing message area.

        Created HIDDEN and kept hidden while its text is empty -- which
        is the overwhelmingly normal state for every one of them. Hidden
        windows are skipped by wx.GridBagSizer.CalcMin(), and a row that
        ends up zero-height is then dropped along with its vgap, so an
        empty message costs this window literally nothing -- instead of
        the ~18px an always-present (but blank) StaticText reserved
        forever. Live-reported: with six of these across the two panels,
        that was 108px of permanently blank window.

        The fixed LABEL_WRAP_PX width is what caps how wide a message may
        make its panel (wx gives an explicitly-sized window's min size
        priority over its best size), so long text wraps to more lines
        rather than stretching the window sideways -- unchanged from
        before; only the height behaviour is new.

        Use _set_message()/_update_message_row() to fill one in; never
        SetLabel() it directly, or the row would stay collapsed and the
        message would never be seen."""
        text = self._new_message_text(parent, colour)
        grid.Add(text, pos=(row, 0), span=(1, 4), flag=wx.EXPAND)
        return text, row + 1

    def _new_message_text(self, parent: wx.Window, colour: Optional[wx.Colour] = None) -> wx.StaticText:
        """The collapsing message label itself, for the one caller whose
        sizer isn't a GridBagSizer (the Mock Rig Control panel's own
        error line, on a plain BoxSizer). _add_message_row() is the usual
        entry point."""
        text = wx.StaticText(parent, label="", size=(LABEL_WRAP_PX, -1))
        if colour is not None:
            text.SetForegroundColour(colour)
        text.Show(False)
        self._message_text[text] = ""
        return text

    def _update_message_row(self, text_ctrl: wx.StaticText, message: str) -> bool:
        """Sets one collapsing message row's text and its matching
        shown/hidden state. Returns whether anything actually changed --
        the caller owes a _resize_main_window_to_content() if (and only
        if) it did, so a batch of these costs one relayout rather than
        one each. _set_message() is the single-message shorthand.

        The no-change fast path matters: _apply_snapshot() runs on every
        engine tick and rewrites all of these (almost always with the
        same value, usually ""), and resizing this window on each of
        those would be a permanent low-grade flicker.

        self._message_text, not text_ctrl.GetLabel(), holds the
        comparison value because wx.StaticText.Wrap() re-SetLabel()s the
        control with the line-broken text, so GetLabel() no longer
        returns what was set -- comparing against it would make every
        wrapped message look changed on every tick."""
        message = message or ""
        if self._message_text.get(text_ctrl, "") == message:
            return False
        self._message_text[text_ctrl] = message
        _set_wrapped(text_ctrl, message)
        text_ctrl.Show(bool(message))
        return True

    def _set_message(self, text_ctrl: wx.StaticText, message: str) -> None:
        """_update_message_row() plus the relayout it owes, for the call
        sites that set a single message. Passing "" collapses the row
        again. No-ops entirely when the text is unchanged."""
        if self._update_message_row(text_ctrl, message):
            # Same contract _update_mock_rig_panel_visibility() follows
            # for the Mock Rig Control panel: a visibility change inside
            # a fixed-size, content-sized window is only visible once the
            # window itself is re-fitted to the content.
            self._resize_main_window_to_content()

    # ------------------------------------------------------------------ WebSDR panel
    def _on_site_selected(self, _event=None) -> None:
        # A Test result (or a stale error already shown from a previous
        # site) describes whatever was selected when it ran -- leaving it
        # on screen after switching sites is misleading, since it reads as
        # if it still applies to the newly selected one.
        self._set_message(self.websdr_preflight_text, "")
        self._update_websdr_controls()

    def _on_custom_url_edited(self, _event=None) -> None:
        self._set_message(self.websdr_preflight_text, "")
        self._update_websdr_controls()

    def _update_websdr_controls(self) -> None:
        is_custom = self.site_combo.GetValue() == CUSTOM_URL_SENTINEL
        self.custom_url_entry.Enable(is_custom)
        self.detect_btn.Enable(is_custom)

        # Save to list: only offer once this exact Custom URL has actually
        # connected -- saving an unreachable/untested URL would just
        # pollute the dropdown with something that doesn't work.
        selected = self._resolve_selected_site()
        can_save = (
            is_custom
            and self._websdr_active
            and self._active_websdr_site is not None
            and selected is not None
            and selected.url == self._active_websdr_site.url
            and self._websdr_connected
            and not self._site_already_saved(selected)
        )
        self.save_site_btn.Enable(can_save)

        # Delete: only sites the user saved themselves can be removed --
        # KNOWN_SITES are the app's built-in defaults, not user data.
        can_delete = not is_custom and any(s.name == self.site_combo.GetValue() for s in self._user_sites)
        self.delete_site_btn.Enable(can_delete)

        if not self._websdr_active:
            # Connecting a WebSDR before the transceiver is up means the
            # first frequency it reports has no rig context yet (and any
            # out-of-range rejection reads as a mystery instead of an
            # explainable "the rig wants a frequency this profile doesn't
            # cover") -- require the rig connected first. Gated on
            # _rig_connected (actually up and answering), not _rig_active
            # (a session merely started) -- clicking Connect while rigctld
            # is still mid-handshake let a rig-connect timeout tear the
            # WebSDR window down while it was still opening.
            self.websdr_connect_btn.SetLabel("Connect")
            self.websdr_connect_btn.Enable(self._rig_connected)
            self.headless_check.Enable(True)
            self.cw_offset_ctrl.Enable(True)
            return

        self.headless_check.Enable(False)
        self.cw_offset_ctrl.Enable(False)
        # One button, two meanings while connected -- "Disconnect" when
        # the dropdown still points at what's active, "Load" when a
        # different site is selected (loads it on the SAME already-open
        # WebSDR window; see SyncEngine._switch_websdr()). No separate
        # always-there Disconnect button any more, and no in-place
        # destroy-and-recreate "Switch" either -- an earlier version of
        # that hand-off (going through a real disconnect+reconnect) was
        # traced to four distinct, hard-to-fully-close Win32 races in one
        # live-testing session (z-order corruption against a maximized
        # app, the window going invisible-with-audio-still-playing, a
        # stale engine-tick snapshot defeating the reconnect, and a
        # rig-disconnect racing the reconnect's own rig-connected gate).
        # Reusing the page instead of tearing the window down avoids all
        # of that, since the window itself never hides or needs its
        # z-order re-established.
        active = self._active_websdr_site
        same_as_active = (
            selected is not None and active is not None
            and selected.url == active.url and selected.driver_type == active.driver_type
        )
        if same_as_active:
            self.websdr_connect_btn.SetLabel("Disconnect")
        else:
            self.websdr_connect_btn.SetLabel("Load")
        self.websdr_connect_btn.Enable(True)

    def _on_websdr_window_close_requested(self) -> None:
        """The WebSDR popup's own X button (see WebViewHost.on_close_requested)
        -- the frame's close is always vetoed there, so this is the only
        thing a click on it ever does. Same effect as the Connect/Disconnect
        toggle button while active; a no-op if nothing is active (e.g. a
        stray click arriving just as a disconnect completes on its own)."""
        self._disconnect_websdr()

    def _on_reverse_sync_hold_toggled(self, _event=None) -> None:
        # Not gated on _websdr_active -- deliberately toggleable before
        # connecting, so it can be pre-armed (see engine.set_reverse_sync_held's
        # docstring for why setting it before the engine loop exists is safe).
        self.engine.set_reverse_sync_held(self.reverse_sync_hold_btn.GetValue())

    def _websdr_connect_tooltip_text(self) -> Optional[str]:
        # The only reason Connect is ever disabled (as opposed to clickable
        # but rejected with an error, e.g. an undetected Custom URL) is the
        # rig-first gate -- explain that on hover rather than leaving a
        # greyed-out button with no clue why.
        if not self.websdr_connect_btn.IsEnabled() and not self._rig_connected:
            return "Connect the transceiver first"
        return None

    def _resolve_selected_site(self) -> Optional[WebSDRSite]:
        """Returns the WebSDRSite the WebSDR panel's controls currently
        point at, or None if the selection is invalid (unrecognized name,
        or Custom URL selected but not yet successfully detected). Callers
        must show an error and refuse to proceed on None -- never silently
        fall back to a different site than what's selected."""
        name = self.site_combo.GetValue()
        if name == CUSTOM_URL_SENTINEL:
            if self._custom_site is not None and self._custom_site.url == self.custom_url_entry.GetValue().strip():
                return self._custom_site
            return None
        return next((s for s in self._all_selectable_sites() if s.name == name), None)

    def _find_any_site_by_url(self, url: str) -> Optional[WebSDRSite]:
        # Deliberately scoped to KNOWN_SITES + self._user_sites only, NOT
        # the wider _all_selectable_sites() set -- this drives can_save's
        # "already saved" check and last_site_driver_type's restart-
        # persistence decision (_on_websdr_connect_clicked), both of which
        # manage the user's OWN saved-site bookkeeping. Widening this
        # would make can_save go permanently False for a Custom URL that
        # happens to match a curated/imported entry, and would make a
        # restart-persisted site vanish silently if a later "Update from
        # GitHub" replace-all removes the matching curated entry.
        return next((s for s in KNOWN_SITES + self._user_sites if s.url == url), None)

    def _site_already_saved(self, site: WebSDRSite) -> bool:
        return self._find_any_site_by_url(site.url) is not None

    def _all_selectable_sites(self) -> list[WebSDRSite]:
        # Wider than _find_any_site_by_url's scope -- used only where the
        # dropdown/name-lookup genuinely needs to see curated/imported
        # sites too (_refresh_site_dropdown_values, _resolve_selected_site).
        return KNOWN_SITES + self._user_sites + self._curated_sites + self._imported_sites

    def _find_selectable_site_by_url(self, url: str) -> Optional[WebSDRSite]:
        """Same idea as _find_any_site_by_url() but over the FULL
        _all_selectable_sites() scope -- for "does the dropdown already
        have an entry for this URL, so it can just be selected by name"
        questions (the last-used-site restore below, and the matching
        save-time decision of whether last_site_driver_type needs to
        remember a synthetic Custom-URL reconstruction at all). Connecting
        to a curated/imported site (most of the built-in list) previously
        fell outside _find_any_site_by_url's narrower KNOWN_SITES+
        user_sites scope, so it got needlessly remembered and restored as
        a Custom URL instead of just reselecting the dropdown entry that
        was right there the whole time -- live-reported after a session
        spent on OH6LSL (a curated-list site)."""
        return next((s for s in self._all_selectable_sites() if s.url == url), None)

    def _refresh_site_dropdown_values(self) -> None:
        names = [s.name for s in self._all_selectable_sites()] + [CUSTOM_URL_SENTINEL]
        current = self.site_combo.GetValue()
        self.site_combo.Set(names)
        if current in names:
            self.site_combo.SetValue(current)
        else:
            # A replace-all (Update from GitHub / re-Load) can drop the
            # currently-selected/active site out from under the dropdown
            # -- never leave the combo with no selection at all.
            self.site_combo.SetValue(KNOWN_SITES[0].name)

    def _persist_user_sites(self) -> None:
        self.settings.user_sites = [
            {"name": s.name, "url": s.url, "driver_type": s.driver_type} for s in self._user_sites
        ]
        self.settings.save()

    def _save_site_tooltip_text(self) -> Optional[str]:
        if not self.save_site_btn.IsEnabled():
            return "Connect this Custom URL successfully first, then Save to list"
        return None

    def _delete_site_tooltip_text(self) -> Optional[str]:
        if not self.delete_site_btn.IsEnabled():
            return "Only sites you've saved to the list can be deleted"
        return None

    def _on_save_site_clicked(self, _event=None) -> None:
        site = self._resolve_selected_site()
        if site is None or self._site_already_saved(site):
            return
        self._user_sites.append(site)
        self._persist_user_sites()
        self._refresh_site_dropdown_values()
        self.detect_result_text.SetLabel(f"Saved to list: {site.name}")
        self._update_websdr_controls()

    def _on_delete_site_clicked(self, _event=None) -> None:
        name = self.site_combo.GetValue()
        site = next((s for s in self._user_sites if s.name == name), None)
        if site is None:
            return  # built-in sites can't be deleted -- guarded by button state too
        confirm = wx.MessageBox(
            f"Remove '{site.name}' from the list?", "Delete WebSDR site",
            wx.YES_NO | wx.ICON_QUESTION, self,
        )
        if confirm != wx.YES:
            return
        self._user_sites = [s for s in self._user_sites if s.url != site.url]
        self._persist_user_sites()
        self._refresh_site_dropdown_values()
        # The deleted entry can no longer be selected.
        self.site_combo.SetValue(KNOWN_SITES[0].name)
        self._on_site_selected()

    def _on_manage_sites_clicked(self, _event=None) -> None:
        # existing_sites matches _find_any_site_by_url's scope exactly
        # (KNOWN_SITES + user_sites), not _all_selectable_sites() -- the
        # dialog's collision check needs to know about the user's own
        # saved sites, not the curated/imported buckets it's about to
        # replace/extend itself.
        dlg = SiteManagerDialog(self, self.settings, KNOWN_SITES + self._user_sites)
        dlg.ShowModal()
        dlg.Destroy()
        # The dialog writes replace-all results straight into
        # self.settings' buckets -- rebuild our in-memory mirrors and
        # refresh the dropdown now that it's closed.
        self._imported_sites = [
            WebSDRSite(name=d["name"], url=d["url"], driver_type=d["driver_type"])
            for d in self.settings.imported_sites
        ]
        self._curated_sites = [
            WebSDRSite(name=d["name"], url=d["url"], driver_type=d["driver_type"])
            for d in self.settings.curated_sites
        ]
        self._refresh_site_dropdown_values()
        self._update_websdr_controls()

    def _maybe_auto_update_curated_sites(self) -> None:
        # Fresh installs (or a curated bucket the user emptied entirely via
        # "Remove selected") otherwise stay at just the 3 built-in
        # KNOWN_SITES until someone opens "Manage sites..." and clicks
        # Update -- do that once automatically instead, reusing the exact
        # same background-fetch machinery the dialog's own button uses
        # (SiteManagerDialog._run_fetch is a plain staticmethod with no
        # `self` dependency). Silent-if-offline: this is a background
        # convenience fetch the user didn't explicitly ask for, unlike the
        # dialog's own button click, which does show its failure message.
        if self.settings.curated_sites:
            return
        thread = threading.Thread(
            target=SiteManagerDialog._run_fetch,
            args=("curated", CURATED_LIST_URL, KNOWN_SITES + self._user_sites, self.status_queue),
            daemon=True,
        )
        thread.start()

    def _apply_curated_autofetch_result(self, result: SiteListFetchResult) -> None:
        if result.sites is None:
            logger.info("Background curated-site auto-fetch did not complete: %s", result.message)
            return
        self.settings.curated_sites = result.sites
        self.settings.save()
        self._curated_sites = [
            WebSDRSite(name=d["name"], url=d["url"], driver_type=d["driver_type"])
            for d in self.settings.curated_sites
        ]
        # GUI REWRITE: the new Sites panel keeps its own copy of the
        # curated/imported buckets (loaded once at construction, same as
        # this frame's own _curated_sites above) -- resync it now rather
        # than the old _refresh_site_dropdown_values(), which targeted
        # the removed self.site_combo. StripPanel's own chooser is
        # intentionally NOT widened by a background auto-fetch (only by
        # an explicit Sites-panel Load, see _on_sites_panel_load) -- it
        # only ever needs to already contain the site the user is about
        # to connect to, which curated entries reach via Load, not
        # automatically.
        if hasattr(self, "sites_panel"):
            self.sites_panel.sync_from_settings()
        logger.info("Background curated-site auto-fetch populated %d site(s)", len(result.sites))

    def _restore_custom_site_if_needed(self) -> None:
        if self._find_selectable_site_by_url(self.settings.last_site_url) is not None:
            return
        if not self.settings.last_site_driver_type or not self.settings.last_site_url:
            return
        self._custom_site = WebSDRSite(
            name=f"Custom ({self.settings.last_site_driver_type}): {self.settings.last_site_url}",
            url=self.settings.last_site_url,
            driver_type=self.settings.last_site_driver_type,
        )
        self.site_combo.SetValue(CUSTOM_URL_SENTINEL)
        self.custom_url_entry.SetValue(self.settings.last_site_url)
        self.detect_result_text.SetLabel(f"Restored: {self.settings.last_site_driver_type}")
        self._update_websdr_controls()

    def _on_detect_clicked(self, _event=None) -> None:
        if self.detect_thread is not None and self.detect_thread.is_alive():
            return  # a detect is already in flight; ignore double-clicks
        url = self.custom_url_entry.GetValue().strip()
        if not url:
            self.detect_result_text.SetLabel("Enter a URL first")
            return
        self._custom_site = None
        self.detect_btn.Enable(False)
        self.detect_btn.SetLabel("Detecting...")
        self.detect_result_text.SetLabel(f"Detecting WebSDR type at {url}...")
        self.detect_thread = threading.Thread(
            target=self._run_detect, args=(url, self.status_queue), daemon=True
        )
        self.detect_thread.start()

    @staticmethod
    def _run_detect(url: str, status_queue: "queue.Queue") -> None:
        try:
            driver_type, message = asyncio.run(detect_websdr_type(url))
        except Exception as e:
            logger.exception("Detect check crashed")
            driver_type, message = None, f"Detect failed: {e}"
        try:
            status_queue.put_nowait(DetectResult(url, driver_type, message))
        except queue.Full:
            pass

    def _apply_detect_result(self, result: DetectResult) -> None:
        self.detect_btn.SetLabel("Detect")
        if not self._websdr_active:
            self._update_websdr_controls()
        if result.url != self.custom_url_entry.GetValue().strip():
            # The URL field was edited (or cleared) while this check was in
            # flight -- this result no longer describes what's in the field,
            # so don't let it set _custom_site. Leave the label as-is rather
            # than showing a stale/misleading message for a URL that's no
            # longer visible.
            return
        self.detect_result_text.SetLabel(result.message)
        if result.driver_type is not None:
            self._custom_site = WebSDRSite(
                name=f"Custom ({result.driver_type}): {result.url}", url=result.url, driver_type=result.driver_type,
            )
        else:
            self._custom_site = None
        self._update_websdr_controls()

    def _on_websdr_test_clicked(self, _event=None) -> None:
        if self.websdr_test_thread is not None and self.websdr_test_thread.is_alive():
            return
        site = self._resolve_selected_site()
        if site is None:
            self._set_message(self.websdr_preflight_text, "Select a known site, or Detect a Custom URL first")
            return
        self.websdr_test_btn.Enable(False)
        self.websdr_test_btn.SetLabel("Testing...")
        self._set_message(self.websdr_preflight_text, "Checking WebSDR reachability...")
        self.websdr_test_thread = threading.Thread(
            target=self._run_websdr_test, args=(site.url, self.status_queue), daemon=True
        )
        self.websdr_test_thread.start()

    @staticmethod
    def _run_websdr_test(url: str, status_queue: "queue.Queue") -> None:
        try:
            ok, message = asyncio.run(check_websdr_url(url))
        except Exception as e:
            logger.exception("WebSDR preflight check crashed")
            ok, message = False, f"Check failed: {e}"
        try:
            status_queue.put_nowait(WebsdrPreflightResult(ok, message))
        except queue.Full:
            pass

    def _apply_websdr_preflight(self, result: WebsdrPreflightResult) -> None:
        self.websdr_test_btn.Enable(True)
        self.websdr_test_btn.SetLabel("Test")
        self._set_message(self.websdr_preflight_text, ("OK: " if result.ok else "FAIL: ") + result.message)

    def _on_websdr_connect_clicked(self, _event=None) -> None:
        if self._websdr_active:
            # Disconnect if the dropdown still matches what's active,
            # Load if a different site is selected -- see
            # _update_websdr_controls() for the same comparison driving
            # the button's label, and _begin_websdr_switch()/
            # SyncEngine._switch_websdr() for why this reuses the
            # existing window instead of disconnecting first.
            active = self._active_websdr_site
            site = self._resolve_selected_site()
            if site is not None and active is not None and site.url == active.url and site.driver_type == active.driver_type:
                self._disconnect_websdr()
                return
            if site is None:
                self._set_message(self.websdr_err_text, "Select a known site, or Detect a Custom URL first")
                return
            self._begin_websdr_switch(site)
            return

        if not self._rig_connected:
            # Belt-and-suspenders -- the button is disabled in this state
            # (see _update_websdr_controls()), but guard the handler too in
            # case of a race between a click and a state-changing snapshot.
            self._set_message(self.websdr_err_text, "Connect the transceiver first")
            return

        site = self._resolve_selected_site()
        if site is None:
            self._set_message(self.websdr_err_text, "Select a known site, or Detect a Custom URL first")
            return

        self._begin_websdr_connect(site)

    # ------------------------------------------------------------------ Transceiver panel
    def _on_mock_rig_toggled(self, _event=None) -> None:
        is_mock = self.mock_rig_check.GetValue()
        self.host_entry.Enable(not is_mock)

    def _populate_host_port_for_backend(self, backend: str) -> None:
        if backend == "flrig":
            self.host_entry.SetValue(self.settings.flrig_host)
            self.port_entry.SetValue(str(self.settings.flrig_port))
        else:
            self.host_entry.SetValue(self.settings.rigctld_host)
            self.port_entry.SetValue(str(self.settings.rigctld_port))

    def _save_current_backend_host_port(self) -> None:
        """Persists whatever's currently typed into host_entry/port_entry
        back into the *currently selected* backend's own settings fields
        -- call this before switching self.settings.rig_backend to a new
        value, so each backend keeps its own host/port across switches
        instead of one clobbering the other."""
        host = self.host_entry.GetValue().strip() or "127.0.0.1"
        try:
            port = int(self.port_entry.GetValue())
        except ValueError:
            return  # don't clobber a saved port with garbage; leave it as-is
        if self.settings.rig_backend == "flrig":
            self.settings.flrig_host, self.settings.flrig_port = host, port
        else:
            self.settings.rigctld_host, self.settings.rigctld_port = host, port

    def _on_rig_backend_selected(self, _event=None) -> None:
        self._save_current_backend_host_port()
        new_backend = self.rig_backend_combo.GetValue()
        self.settings.rig_backend = new_backend
        self.settings.save()
        self._populate_host_port_for_backend(new_backend)

    def _on_cw_offset_changed(self, _event=None) -> None:
        # Persist on every edit, not just at Connect time -- otherwise an
        # unsaved edit could be silently overwritten by an unrelated
        # settings.save() elsewhere (e.g. changing poll_interval_s) before
        # the user ever clicks Connect. Reconnect is still required for
        # the engine to actually pick up a new offset (only read once, at
        # driver construction) -- this only fixes persistence, not that.
        self.settings.cw_offset_hz = self.cw_offset_ctrl.GetValue()
        self.settings.save()

    def _on_mute_on_tx_changed(self, _event=None) -> None:
        # sync/engine.py reads self.settings.mute_on_tx live on each PTT
        # edge (mirrors _on_poll_interval_changed's immediate-write pattern),
        # so this applies without a reconnect.
        self.settings.mute_on_tx = self.mute_on_tx_check.GetValue()
        self.settings.save()

    def _on_poll_interval_changed(self, _event=None) -> None:
        # SyncEngine reads self.settings.poll_interval_s live on every tick
        # (sync/engine.py's _poll_loop), so this applies immediately --
        # including mid-session -- with no reconnect needed.
        self.settings.poll_interval_s = self.poll_interval_ctrl.GetValue()
        self.settings.save()

    def _refresh_reverse_sync_range_fields(self) -> None:
        """Redisplay both range fields from the saved settings, so what's
        on screen is always exactly what's being enforced. ChangeValue(),
        not SetValue(), so this never re-fires a text event."""
        for entry, value in (
            (self.reverse_sync_min_entry, self.settings.reverse_sync_min_hz),
            (self.reverse_sync_max_entry, self.settings.reverse_sync_max_hz),
        ):
            text = "" if value is None else str(value)
            if entry.GetValue() != text:
                entry.ChangeValue(text)

    def _on_reverse_sync_range_commit(self, event=None) -> None:
        """Commit on Enter/blur, NOT per keystroke. sync/engine.py reads
        self.settings.reverse_sync_min_hz/max_hz live on every reverse-sync
        tick, so anything saved here is enforced within one poll interval
        -- and this range is what bounds the frequencies a public WebSDR
        page may write to a real transmitter. A per-keystroke save would
        therefore persist and enforce every mid-edit state (a field
        momentarily blank while retyping, a half-typed number, a pasted
        "14,000,000"), most of which read as "unrestricted". That's fine
        for a cosmetic field and not fine for this one.

        Blank stays a valid, intentional state (None/unrestricted). A
        genuinely unparseable value saves nothing and reverts the fields
        to the last saved values, so the screen never shows a bound that
        isn't actually in force. Otherwise the values go through
        config.clamp_reverse_sync_bounds() -- the same negative-drop /
        inverted-swap rules load() applies to a hand-edited config.json,
        called rather than reimplemented so the two layers can't drift --
        and the fields are redisplayed from what was actually saved, so a
        corrected (swapped) range doesn't leave the raw typed text on
        screen misrepresenting what's enforced."""
        if event is not None:
            event.Skip()  # EVT_KILL_FOCUS must keep propagating
        min_hz, min_valid = _parse_optional_hz(self.reverse_sync_min_entry.GetValue())
        max_hz, max_valid = _parse_optional_hz(self.reverse_sync_max_entry.GetValue())
        if not (min_valid and max_valid):
            logger.warning("Ignoring unparseable reverse-sync range input; reverting to the saved values")
            self._refresh_reverse_sync_range_fields()
            return
        min_hz, max_hz = clamp_reverse_sync_bounds(min_hz, max_hz)
        # Blur fires on every tab-through, so skip the disk write when
        # nothing actually changed; the redisplay below still runs, which
        # is what normalizes e.g. surrounding whitespace.
        if (min_hz, max_hz) != (self.settings.reverse_sync_min_hz, self.settings.reverse_sync_max_hz):
            self.settings.reverse_sync_min_hz = min_hz
            self.settings.reverse_sync_max_hz = max_hz
            self.settings.save()
        self._refresh_reverse_sync_range_fields()

    def _refresh_idle_disconnect_field(self) -> None:
        """Redisplay the field from the saved setting, so what's on screen
        is always exactly what's in force. ChangeValue(), not SetValue(),
        so this never re-fires a text event."""
        value = self.settings.websdr_idle_disconnect_min
        text = "" if value is None else str(value)
        if self.idle_disconnect_entry.GetValue() != text:
            self.idle_disconnect_entry.ChangeValue(text)

    def _on_idle_disconnect_commit(self, event=None) -> None:
        """Commit on Enter/blur, NOT per keystroke -- mirrors
        _on_reverse_sync_range_commit exactly. sync/engine.py reads
        self.settings.websdr_idle_disconnect_min live on every tick, so
        anything saved here takes effect within one poll interval; a
        per-keystroke save would briefly enforce every mid-edit state
        (e.g. a lone "6" while typing "60" would arm a 6-minute
        disconnect). An unparseable value saves nothing and reverts,
        rather than silently collapsing to "disabled"."""
        if event is not None:
            event.Skip()  # EVT_KILL_FOCUS must keep propagating
        minutes, valid = _parse_optional_minutes(self.idle_disconnect_entry.GetValue())
        if not valid:
            logger.warning("Ignoring unparseable idle-disconnect input; reverting to the saved value")
            self._refresh_idle_disconnect_field()
            return
        minutes = clamp_idle_disconnect_min(minutes)
        # Blur fires on every tab-through, so skip the disk write when
        # nothing actually changed.
        if minutes != self.settings.websdr_idle_disconnect_min:
            self.settings.websdr_idle_disconnect_min = minutes
            self.settings.save()
        self._refresh_idle_disconnect_field()

    def _update_mock_rig_panel_visibility(self) -> None:
        # Gate on the *running rig session's* mode, not the live checkbox
        # -- the checkbox is disabled while the rig is connected so they
        # can't diverge, but this is the real invariant: a real-rig session
        # must never show controls that look like they drive a real radio,
        # even transiently.
        should_show = self._rig_active and self.settings.use_mock_rig
        if self.mock_box.IsShown() != should_show:
            self.mock_box.Show(should_show)
            # Grows/shrinks this window live to fit whichever of
            # this-panel-shown/hidden is now current, clamped to the
            # display and never below the "Open log folder"-visible
            # floor -- see _resize_main_window_to_content()'s docstring.
            self._resize_main_window_to_content()

    def _on_rig_test_clicked(self, _event=None) -> None:
        if self.rig_test_thread is not None and self.rig_test_thread.is_alive():
            return
        try:
            port = int(self.port_entry.GetValue())
        except ValueError:
            self._set_message(self.rig_preflight_text, "Invalid port")
            return
        host = self.host_entry.GetValue().strip() or "127.0.0.1"
        backend = self.rig_backend_combo.GetValue()
        self.rig_test_btn.Enable(False)
        self.rig_test_btn.SetLabel("Testing...")
        self._set_message(self.rig_preflight_text, f"Checking {backend} reachability...")
        self.rig_test_thread = threading.Thread(
            target=self._run_rig_test, args=(backend, host, port, self.status_queue), daemon=True
        )
        self.rig_test_thread.start()

    @staticmethod
    def _run_rig_test(backend: str, host: str, port: int, status_queue: "queue.Queue") -> None:
        try:
            check = check_flrig if backend == "flrig" else check_rigctld
            ok, message = asyncio.run(check(host, port))
        except Exception as e:
            logger.exception("Rig preflight check crashed")
            ok, message = False, f"Check failed: {e}"
        try:
            status_queue.put_nowait(RigPreflightResult(ok, message))
        except queue.Full:
            pass

    def _apply_rig_preflight(self, result: RigPreflightResult) -> None:
        self.rig_test_btn.Enable(True)
        self.rig_test_btn.SetLabel("Test")
        self._set_message(self.rig_preflight_text, ("OK: " if result.ok else "FAIL: ") + result.message)

    def _on_rig_connect_clicked(self, _event=None) -> None:
        if self._rig_active:
            self.rig_connect_btn.Enable(False)
            self.rig_connect_btn.SetLabel("Disconnecting...")
            self.engine.stop_rig_from_other_thread()
            return

        try:
            port = int(self.port_entry.GetValue())
        except ValueError:
            self._set_message(self.rig_err_text, "Invalid port")
            return

        backend = self.rig_backend_combo.GetValue()
        use_mock = self.mock_rig_check.GetValue()
        # A mock rig only ever exists on loopback -- ignore whatever's typed
        # in the host field rather than silently trying to bind a mock
        # server to some other address.
        host = "127.0.0.1" if use_mock else (self.host_entry.GetValue().strip() or "127.0.0.1")

        if backend == "flrig":
            self.settings.flrig_host = host
            self.settings.flrig_port = port
        else:
            self.settings.rigctld_host = host
            self.settings.rigctld_port = port
        self.settings.rig_backend = backend
        self.settings.use_mock_rig = use_mock
        self.settings.save()

        self._rig_ever_connected = False
        self.rig_conn_text.SetLabel("connecting...")
        self._set_message(self.rig_err_text, "")
        self.rig_connect_btn.Enable(False)
        self.rig_connect_btn.SetLabel("Connecting...")
        self.mock_rig_check.Enable(False)
        self.host_entry.Enable(False)
        self.port_entry.Enable(False)
        self.rig_backend_combo.Enable(False)
        self.engine.start_rig_from_other_thread(backend, host, port, use_mock)

    # ------------------------------------------------------------------
    def _poll_status_queue(self, _event=None) -> None:
        try:
            while True:
                item = self.status_queue.get_nowait()
                handler = self._dispatch.get(type(item))
                if handler is None:
                    logger.warning("No GUI handler registered for status_queue message type %r", type(item))
                    continue
                handler(item)
        except queue.Empty:
            pass

    def _apply_status_snapshot(self, snap: StatusSnapshot) -> None:
        """GUI REWRITE: drives AppState from a real StatusSnapshot -- the
        phase-6 replacement for the old widget-mutating _apply_snapshot()
        below (kept, unused, for reference until the phase 10 cleanup).
        A full build_app_state()-style extraction (spec §8) lands in
        phase 8 alongside the new `paused` engine flag; this is the
        minimum needed to reconnect the engine now. Mirrors the old
        method's _websdr_connect_pending gate exactly (see that flag's
        own docstring above) so a stale "nothing to report" tick mid-
        connect can't be mistaken for a real disconnect."""
        if snap.fatal_error:
            self._rig_active = False
            self._websdr_active = False
            self._websdr_connect_pending = False
            self._strip_connect_busy_label = None
            self._active_websdr_site = None
            self._state.rig_connected = False
            self._state.sdr_connected = False
            self._state.sdr_active = False
            self.transceiver_panel.set_connection_state(False)
            self._refresh_chrome()
            return

        self._rig_active = snap.rig_active
        self._state.rig_connected = bool(snap.rig_active and snap.rig_connected)
        if snap.rig_active:
            self._rig_ever_connected = self._rig_ever_connected or snap.rig_connected
            self._state.rx_hz = snap.rig_freq_hz or 0
            self._state.tx_hz = snap.rig_freq_hz or 0
            self._state.mode = snap.rig_mode or self._state.mode
            self._state.ptt = bool(snap.rig_ptt)
        else:
            self._state.rx_hz = 0
            self._state.tx_hz = 0
            self._state.ptt = False
        self.transceiver_panel.set_connection_state(snap.rig_active)

        self._websdr_active = snap.websdr_active
        self._state.sdr_active = snap.websdr_active
        if not snap.websdr_active:
            if not self._websdr_connect_pending or snap.websdr is not None:
                self._websdr_connect_pending = False
                self._strip_connect_busy_label = None
                self._active_websdr_site = None
                self._state.sdr_connected = False
                self._state.sdr_hz = 0
                self._state.site = ""
            else:
                # Still mid-connect (pending, no explicit failure yet) --
                # keep ReceiverHost on Live so the WebView stays part of
                # the visible tree throughout the attempt, not just once
                # it succeeds. See AppState.sdr_active's own docstring.
                self._state.sdr_active = True
        else:
            self._websdr_connect_pending = False
            self._strip_connect_busy_label = None
            ws = snap.websdr
            self._state.sdr_connected = bool(ws and ws.connected)
            if ws is not None:
                self._state.sdr_hz = ws.freq_hz or 0
                if self._active_websdr_site is not None:
                    self._state.site = self._active_websdr_site.name

        self._refresh_chrome()

    def _apply_snapshot(self, snap: StatusSnapshot) -> None:
        # Every message row this method may touch is updated through
        # _update_message_row() and this one accumulator, so a snapshot
        # that changes several of them (or, far more often, none of them
        # -- this runs on every engine tick) costs at most one window
        # relayout. See _update_message_row()'s docstring.
        messages_changed = False
        if snap.fatal_error:
            # The whole background thread has died -- both subsystems are
            # gone with it and there's nothing to salvage short of
            # restarting the app. Distinct from (and far rarer than) either
            # subsystem's own start failing, which is reported through its
            # own error field further down instead.
            messages_changed |= self._update_message_row(
                self.websdr_err_text, f"Sync engine crashed: {snap.fatal_error}")
            messages_changed |= self._update_message_row(
                self.rig_err_text, f"Sync engine crashed: {snap.fatal_error}")
            self.websdr_conn_text.SetLabel("error")
            self.websdr_driver_text.SetLabel("-")
            self.rig_conn_text.SetLabel("error")
            messages_changed |= self._update_message_row(self.reverse_sync_error_text, "")
            messages_changed |= self._update_message_row(self.reverse_sync_pending_text, "")
            self.reverse_sync_held_text.SetLabel("")
            self.websdr_idle_text.SetLabel("")
            self.reverse_sync_hold_btn.Enable(False)
            self._websdr_active = False
            self._websdr_connected = False
            self._websdr_connect_pending = False
            self._save_current_webview_geometry()
            self._active_websdr_site = None
            self._webview_host.set_frame_visible(False)
            self._update_websdr_controls()
            # Override _update_websdr_controls' normal rig-active-based
            # enabling -- the whole engine thread is dead, so both connect
            # buttons must stay disabled regardless of stale _rig_active.
            self.websdr_connect_btn.Enable(False)
            self.rig_connect_btn.Enable(False)
            if messages_changed:
                self._resize_main_window_to_content()
            return

        # --- Transceiver ---
        self._rig_active = snap.rig_active
        self._rig_connected = bool(snap.rig_active and snap.rig_connected)
        if snap.rig_active:
            if snap.rig_connected:
                self._rig_ever_connected = True
                self.rig_conn_text.SetLabel("connected")
            else:
                self.rig_conn_text.SetLabel("reconnecting..." if self._rig_ever_connected else "connecting...")
            self.rig_freq_text.SetLabel(f"{snap.rig_freq_hz/1000:.3f} kHz" if snap.rig_freq_hz else "-")
            self.rig_mode_text.SetLabel(snap.rig_mode or "-")
            self.rig_ptt_text.SetLabel("TX" if snap.rig_ptt else ("RX" if snap.rig_ptt is not None else "-"))
            self.rig_connect_btn.Enable(True)
            self.rig_connect_btn.SetLabel("Disconnect")
        else:
            self.rig_conn_text.SetLabel("not connected")
            self.rig_freq_text.SetLabel("-")
            self.rig_mode_text.SetLabel("-")
            self.rig_ptt_text.SetLabel("-")
            self.rig_connect_btn.Enable(True)
            self.rig_connect_btn.SetLabel("Connect")
            self.host_entry.Enable(not self.mock_rig_check.GetValue())
            self.port_entry.Enable(True)
            self.mock_rig_check.Enable(True)
            self.rig_backend_combo.Enable(True)
            self.poll_interval_ctrl.Enable(True)
        messages_changed |= self._update_message_row(self.rig_err_text, snap.rig_error or "")
        self._update_mock_rig_panel_visibility()

        # --- WebSDR ---
        self._websdr_active = snap.websdr_active
        if not snap.websdr_active:
            # A pending Connect's own _start_websdr() hasn't
            # flipped the engine's websdr_active True yet (it's gated on a
            # GUI-thread WebView-creation round-trip), so SyncEngine._tick()
            # keeps publishing its ordinary "nothing to report" snapshots
            # (websdr_active=False, websdr=None) throughout that window --
            # on the wire, indistinguishable from _stop_websdr() having
            # just genuinely completed (it publishes the same shape). Only
            # an explicit failure report (snap.websdr is not None -- see
            # _start_websdr's own error-path publishes) or the absence of
            # a pending attempt makes this a REAL "nothing is connected"
            # state; otherwise, skip the teardown below and leave the
            # connect-in-progress UI/window exactly as the click handler
            # set it. See self._websdr_connect_pending's own docstring.
            if not self._websdr_connect_pending or snap.websdr is not None:
                # Covers every disconnect/failure path that lands here
                # (manual Disconnect completing, idle-disconnect, a
                # connect attempt that definitively failed) -- the
                # fatal_error branch above handles its own case, since it
                # returns before reaching here. Self-guards against
                # repeat snapshots since _active_websdr_site is cleared
                # right below, so this only actually saves/hides once per
                # transition.
                self._websdr_connect_pending = False
                self._save_current_webview_geometry()
                self._active_websdr_site = None
                self._webview_host.set_frame_visible(False)
                # v14: an idle release is a deliberate, healthy state -- it
                # must read as such, not as "not connected" (which looks
                # like the user never connected) and certainly not through
                # the red error label below.
                self._websdr_conn_text = "disconnected (idle)" if snap.websdr_idle_stopped else "not connected"
                self._websdr_connected = False
                self.websdr_conn_text.SetLabel(self._websdr_conn_text)
                self.websdr_driver_text.SetLabel("-")
                self.websdr_freq_text.SetLabel("-")
                self.websdr_mode_text.SetLabel("-")
                self.websdr_audio_text.SetLabel("-")
            messages_changed |= self._update_message_row(
                self.websdr_err_text, snap.websdr.last_error if snap.websdr is not None else "")
        else:
            self._websdr_connect_pending = False
            ws = snap.websdr
            self._websdr_connected = bool(ws and ws.connected)
            self.websdr_driver_text.SetLabel(
                self._active_websdr_site.driver_type if self._active_websdr_site is not None else "-"
            )
            if ws is not None:
                if ws.connected:
                    self._websdr_ever_connected = True
                    self._websdr_conn_text = "connected"
                else:
                    self._websdr_conn_text = "reconnecting..." if self._websdr_ever_connected else "connecting..."
                self.websdr_conn_text.SetLabel(self._websdr_conn_text)
                self.websdr_freq_text.SetLabel(f"{ws.freq_hz/1000:.3f} kHz" if ws.freq_hz else "-")
                self.websdr_mode_text.SetLabel(ws.mode or "-")
                if ws.audio_active is None:
                    self.websdr_audio_text.SetLabel("-")
                else:
                    self.websdr_audio_text.SetLabel("streaming" if ws.audio_active else "silent")
                messages_changed |= self._update_message_row(self.websdr_err_text, ws.last_error or "")
        messages_changed |= self._update_message_row(self.reverse_sync_error_text, snap.reverse_sync_error or "")
        messages_changed |= self._update_message_row(self.reverse_sync_pending_text, snap.reverse_sync_pending or "")
        self.reverse_sync_hold_btn.Enable(True)
        self.reverse_sync_hold_btn.SetValue(snap.reverse_sync_held)
        self.reverse_sync_held_text.SetLabel("Reverse sync held" if snap.reverse_sync_held else "")
        self.websdr_idle_text.SetLabel(
            "Idle -- slot released, will reconnect when you use the rig"
            if snap.websdr_idle_stopped else ""
        )
        self._update_websdr_controls()
        if messages_changed:
            self._resize_main_window_to_content()

    def _on_open_log_folder_clicked(self, _event=None) -> None:
        folder = LOG_FILE.parent
        try:
            if sys.platform == "win32":
                os.startfile(folder)  # noqa: S606 -- no external input involved
            elif sys.platform == "darwin":
                # UNVERIFIED -- no Mac was available to test this path.
                subprocess.run(["open", str(folder)], check=True)
            else:
                subprocess.run(["xdg-open", str(folder)], check=True)
        except (OSError, subprocess.SubprocessError) as e:
            # subprocess.CalledProcessError (nonzero exit) is a
            # SubprocessError, not an OSError -- both must be caught here,
            # unlike os.startfile's single OSError-only failure mode.
            wx.MessageBox(f"Could not open the log folder ({e}).", "SDRSync", wx.OK | wx.ICON_ERROR)

    # ------------------------------------------------------------------
    def _on_close(self, _event=None) -> None:
        # This window's own position -- see _restore_main_window_geometry().
        # No matching size save: see AppSettings.main_window_position's
        # own comment for why this window's height is computed live
        # instead of remembered.
        pos = self.GetPosition()
        self.settings.main_window_position = [pos.x, pos.y]
        self.settings.save()
        # GUI REWRITE IN PROGRESS: engine/thread/timer are None until
        # phase 6 reconnects them (see __init__) -- nothing to stop/join
        # yet in that window.
        if self.engine is not None:
            self.engine.stop_from_other_thread()
            # A plain thread.join() here would block this thread -- but the
            # engine's own shutdown (_stop_websdr() -> WebViewHost.destroy_page())
            # now awaits a future the GUI thread resolves via wx.CallAfter once
            # the old WebView widget is actually destroyed (see webview_host.py).
            # A blocked GUI thread never pumps that CallAfter, so the engine
            # thread would wait the full SHUTDOWN_TIMEOUT_S and never reach
            # _stop_rig() at all -- confirmed live: every close while WebSDR was
            # connected turned into a guaranteed multi-second freeze with the
            # rig socket left uncleanly open. SafeYield() between short joins
            # keeps the event queue (and therefore that CallAfter) moving.
            deadline = time.monotonic() + SHUTDOWN_TIMEOUT_S
            while self.thread.is_alive() and time.monotonic() < deadline:
                wx.SafeYield()
                self.thread.join(timeout=0.05)
        if self._poll_timer is not None:
            self._poll_timer.Stop()
        # GUI REWRITE: the WebView is now a child of this frame (inline
        # embedding, spec §6.1), not a separate top-level frame -- no
        # extra window to explicitly Destroy() any more; this frame's own
        # Destroy() below takes the WebView with it.
        self.Destroy()


def _describe_engine_crash(e: Exception) -> str:
    return str(e)


class SDRSyncApp(wx.App):
    def OnInit(self) -> bool:
        from sdrsync.logging_setup import setup_logging

        setup_logging()
        logger.info("SDRSync %s starting", __version__)
        ensure_webview_backend()
        try:
            assert_backend_available()
        except WebViewBackendUnavailable as e:
            wx.MessageBox(str(e), "SDRSync cannot start", wx.OK | wx.ICON_ERROR)
            return False

        settings = AppSettings.load()
        host = WebViewHost()
        frame = MainFrame(settings, host)
        self.SetTopWindow(frame)
        frame.Show(True)
        # A freshly launched process has no standing "last input event" of
        # its own (that belongs to whatever launched it -- Explorer, a
        # shell, another app), so plain Show()/Raise() can come up behind
        # an already-maximized foreground app exactly like the WebSDR
        # popup used to before Connect started calling
        # bring_pair_to_front() -- see its docstring for why the
        # HWND_TOPMOST/NOTOPMOST band trick this uses doesn't need that
        # permission in the first place, unlike a bare HWND_TOP.
        bring_pair_to_front(frame)
        return True


def main() -> None:
    app = SDRSyncApp(False)
    app.MainLoop()


if __name__ == "__main__":
    main()
