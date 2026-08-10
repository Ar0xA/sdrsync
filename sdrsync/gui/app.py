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
from sdrsync.config import AppSettings, KNOWN_SITES, WebSDRSite
from sdrsync.gui_messages import GuiMessage
from sdrsync.gui import theme
from sdrsync.gui.fonts import load_fonts
from sdrsync.gui.format import fmt_hz
from sdrsync.gui.receiver_host import ReceiverHost
from sdrsync.gui.section_bar import SectionBar
from sdrsync.gui.settings_host import SettingsHost
from sdrsync.gui.settings_panels.behaviour_panel import BehaviourPanel
from sdrsync.gui.settings_panels.sites_panel import SitesPanel
from sdrsync.gui.settings_panels.transceiver_panel import TransceiverPanel
from sdrsync.gui.state import AppState
from sdrsync.gui.status_bar_panel import StatusBarPanel
from sdrsync.gui.strip_panel import StripPanel
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
from sdrsync.sitesource import CURATED_LIST_URL, SiteListFetchResult, fetch_site_list
from sdrsync.sync.engine import StatusSnapshot, SyncEngine

logger = logging.getLogger("sdrsync.gui")

QUEUE_POLL_MS = 150
SHUTDOWN_TIMEOUT_S = 5.0


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
        self._sites_fetch_thread: Optional[threading.Thread] = None

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
        # _apply_status_snapshot).
        self._rig_active = False
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
        self._restore_custom_site_if_needed()
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
        session, if any. Falls back to wx's own default placement both
        when nothing was ever saved and when the saved point no longer
        lands on any connected display (e.g. a docking station's monitor
        unplugged since it was saved).

        Size is not handled here -- spec §2's ReceiverHost (proportion=1)
        absorbs all extra space on resize/maximize, so unlike the
        pre-rewrite content-fitted window there's no separate initial
        sizing pass needed at all; theme.FRAME_SIZE/FRAME_MIN_SIZE (set
        in __init__) are the whole story."""
        pos = self.settings.main_window_position
        if pos and len(pos) == 2:
            candidate = wx.Point(pos[0], pos[1])
            if has_display_at(candidate):
                self.SetPosition(candidate)

    @staticmethod
    def _run_engine(engine: SyncEngine) -> None:
        try:
            asyncio.run(engine.run())
        except Exception as e:
            logger.exception("Sync engine crashed")
            engine.publish_fatal_error(_describe_engine_crash(e))

    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        """spec §2's five-band root layout: StripPanel, SectionBar,
        SettingsHost, ReceiverHost (proportion=1, absorbs all resize
        slack), StatusBarPanel."""
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

        self.status_bar_panel = StatusBarPanel(self)
        self.status_bar_panel.on_open_log_folder = self._on_open_log_folder_clicked
        # Before the first real StatusSnapshot arrives (engine starts
        # after _build_widgets() returns) -- avoids a blank line.
        self.status_bar_panel.set_text("rig not connected")
        self.status_bar_panel.set_dot_mode("disconnected")
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
        # Tells the engine to flip forward-sync pause; the displayed
        # state is read back from the next StatusSnapshot in
        # _apply_status_snapshot, not set here -- mirrors
        # _on_sync_direction_changed's same discipline for reverse sync,
        # so a click can't desync from the engine's actual state.
        if self.engine is not None:
            self.engine.set_forward_sync_paused_from_other_thread(not self._state.paused)
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
            # "Connect WebSDR" -- launches the site currently selected in
            # the strip's own dropdown, exactly like clicking the strip's
            # Connect button (top right) would. Previously just opened
            # the Sites panel, which needed a second click on Load to
            # actually connect -- confirmed as a real bug live, not the
            # intended behavior.
            self._on_strip_connect_clicked()

    def _open_settings_panel(self, key: Optional[str]) -> None:
        self._state.open_panel = key
        self.settings_host.show_panel(key)
        self._refresh_chrome()

    def _on_section_panel_toggled(self, key: str) -> None:
        self._open_settings_panel(None if self._state.open_panel == key else key)

    def _build_settings_panels(self) -> None:
        self.transceiver_panel = TransceiverPanel(self.settings_host, self.settings)
        self.transceiver_panel.on_connect = self._on_transceiver_connect_clicked
        self.transceiver_panel.on_test = self._on_transceiver_test_clicked
        self.transceiver_panel.mock_panel.on_set_freq = self._on_mock_set_freq
        self.transceiver_panel.mock_panel.on_set_mode = self._on_mock_set_mode
        self.transceiver_panel.mock_panel.on_ptt_toggled = self._on_mock_ptt_toggled
        self.settings_host.add_panel("transceiver", self.transceiver_panel)

        self.sites_panel = SitesPanel(self.settings_host, self.settings)
        self.sites_panel.on_load_site = self._on_sites_panel_load
        self.sites_panel.on_detect = self._on_sites_detect_clicked
        self.sites_panel.on_test = self._on_sites_test_clicked
        self.sites_panel.on_fetch = self._on_sites_fetch_requested
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

    # ------------------------------------------------------------------ Sites panel: Detect/Test/Save/More
    def _on_sites_detect_clicked(self, url: str) -> None:
        if self.detect_thread is not None and self.detect_thread.is_alive():
            return  # a detect is already in flight; ignore double-clicks
        self.detect_thread = threading.Thread(
            target=self._run_detect, args=(url, self.status_queue), daemon=True,
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
        self.sites_panel.apply_detect_result(result.url, result.driver_type, result.message)

    def _on_sites_test_clicked(self, url: str) -> None:
        if self.websdr_test_thread is not None and self.websdr_test_thread.is_alive():
            return
        self.websdr_test_thread = threading.Thread(
            target=self._run_websdr_test, args=(url, self.status_queue), daemon=True,
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
        self.sites_panel.apply_test_result(result.ok, result.message)

    def _on_sites_fetch_requested(self, bucket: str, url: str) -> None:
        """Sites panel's More menu: Load from URL / Update from GitHub.
        Reuses the exact same background-fetch machinery as the silent
        first-run curated auto-fetch below (_run_fetch_site_list is a
        plain staticmethod with no `self` dependency) -- the only
        difference is this one is user-triggered, so its result always
        gets a status line (see _apply_curated_autofetch_result)."""
        if self._sites_fetch_thread is not None and self._sites_fetch_thread.is_alive():
            return
        self._sites_fetch_thread = threading.Thread(
            target=self._run_fetch_site_list,
            args=(bucket, url, KNOWN_SITES + self._user_sites, self.status_queue),
            daemon=True,
        )
        self._sites_fetch_thread.start()

    @staticmethod
    def _run_fetch_site_list(bucket: str, url: str, existing: list, status_queue: "queue.Queue") -> None:
        try:
            sites, message = asyncio.run(fetch_site_list(url, existing))
        except Exception as e:
            logger.exception("Site list fetch crashed")
            sites, message = None, f"Fetch failed: {e}"
        try:
            status_queue.put_nowait(SiteListFetchResult(bucket, sites, message))
        except queue.Full:
            pass

    def _maybe_auto_update_curated_sites(self) -> None:
        # Fresh installs (or a curated bucket the user emptied entirely)
        # otherwise stay at just the 3 built-in KNOWN_SITES until someone
        # opens the Sites panel's More menu and clicks Update -- do that
        # once automatically instead, reusing _run_fetch_site_list.
        # Silent-if-offline: this is a background convenience fetch the
        # user didn't explicitly ask for, unlike an explicit More-menu
        # click (see _on_sites_fetch_requested), which does show its
        # failure message.
        if self.settings.curated_sites:
            return
        self._sites_fetch_thread = threading.Thread(
            target=self._run_fetch_site_list,
            args=("curated", CURATED_LIST_URL, KNOWN_SITES + self._user_sites, self.status_queue),
            daemon=True,
        )
        self._sites_fetch_thread.start()

    def _apply_curated_autofetch_result(self, result: SiteListFetchResult) -> None:
        """Handles both the silent first-run curated auto-fetch above and
        a user-triggered Load-from-URL/Update-from-GitHub from the Sites
        panel's More menu (_on_sites_fetch_requested) -- same message
        type either way, told apart only by whether there's a bucket to
        replace (there always is, on success)."""
        if result.sites is not None:
            if result.bucket == "imported":
                self.settings.imported_sites = result.sites
                self._imported_sites = [
                    WebSDRSite(name=d["name"], url=d["url"], driver_type=d["driver_type"]) for d in result.sites
                ]
            else:
                self.settings.curated_sites = result.sites
                self._curated_sites = [
                    WebSDRSite(name=d["name"], url=d["url"], driver_type=d["driver_type"]) for d in result.sites
                ]
            self.settings.save()
            logger.info("Site list fetch (%s) populated %d site(s)", result.bucket, len(result.sites))
        else:
            logger.info("Site list fetch (%s) did not complete: %s", result.bucket, result.message)
        if hasattr(self, "sites_panel"):
            if result.sites is not None:
                self.sites_panel.sync_from_settings()
            self.sites_panel.apply_fetch_result(result.message)

    def _restore_custom_site_if_needed(self) -> None:
        """Restores a previously-connected Custom URL/detected site that
        was never saved to the list, so a restart doesn't silently fall
        back to KNOWN_SITES[0] in the strip's dropdown. Called from
        __init__ AFTER _build_widgets() (_wire_strip_panel()'s dropdown
        build + by-url selection has already run by then), so this
        synthesizes the missing entry into _imported_sites and refreshes
        the strip directly -- mirrors _on_sites_panel_load's
        append-if-missing pattern rather than redoing the initial
        selection logic."""
        if self._find_selectable_site_by_url(self.settings.last_site_url) is not None:
            return
        if not self.settings.last_site_driver_type or not self.settings.last_site_url:
            return
        restored = WebSDRSite(
            name=f"Custom ({self.settings.last_site_driver_type}): {self.settings.last_site_url}",
            url=self.settings.last_site_url,
            driver_type=self.settings.last_site_driver_type,
        )
        self._imported_sites.append(restored)
        names = list(self.strip_panel.site_choice.GetStrings())
        if restored.name not in names:
            names.append(restored.name)
            self.strip_panel.site_choice.Set(names)
        self.strip_panel.site_choice.SetStringSelection(restored.name)

    # ------------------------------------------------------------------ Transceiver panel: Test
    def _on_transceiver_test_clicked(self) -> None:
        if self.rig_test_thread is not None and self.rig_test_thread.is_alive():
            return
        tp = self.transceiver_panel
        backend = tp.backend_choice.GetStringSelection()
        host = tp.host_entry.GetValue().strip() or "127.0.0.1"
        port = tp.port_entry.GetValue()
        tp.begin_test()
        self.rig_test_thread = threading.Thread(
            target=self._run_rig_test, args=(backend, host, port, self.status_queue), daemon=True,
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
        self.transceiver_panel.set_test_result(result.ok, result.message)

    # ------------------------------------------------------------------
    def _all_selectable_sites(self) -> list[WebSDRSite]:
        # Wider than the site-saving/deletion helpers' scope -- used
        # only where the dropdown/name-lookup genuinely needs to see
        # curated/imported sites too (_wire_strip_panel,
        # _resolve_strip_selected_site, _restore_custom_site_if_needed).
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
            self.transceiver_panel.mock_panel.set_enabled(False)
            self.status_bar_panel.set_text(f"Sync engine crashed: {snap.fatal_error}")
            self.status_bar_panel.set_dot_mode("disconnected")
            self._refresh_chrome()
            return

        self._rig_active = snap.rig_active
        self._state.rig_connected = bool(snap.rig_active and snap.rig_connected)
        # Read back from the engine, never set optimistically on click --
        # see _on_pause_toggled -- so a click can't desync the displayed
        # state from what the engine is actually doing.
        self._state.paused = snap.forward_sync_paused
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

        self.transceiver_panel.mock_panel.set_enabled(self._state.sdr_connected)

        text, dot_mode = self._resolve_status_bar_text(snap)
        self.status_bar_panel.set_text(text)
        self.status_bar_panel.set_dot_mode(dot_mode)

        self._refresh_chrome()

    def _rig_status_text(self) -> str:
        if not self._state.rig_connected:
            return "rig not connected"
        if self.settings.use_mock_rig:
            return "mock rig connected"
        backend = self.settings.rig_backend
        if backend == "flrig":
            host, port = self.settings.flrig_host, self.settings.flrig_port
        else:
            host, port = self.settings.rigctld_host, self.settings.rigctld_port
        return f"{backend} connected — {host}:{port}"

    def _resolve_status_bar_text(self, snap: StatusSnapshot) -> tuple[str, str]:
        """spec §8's single-priority status line: exactly one message
        wins, in this order (per the plan's agreed precedence): fatal >
        rig error > WebSDR error > reverse-sync error > paused >
        reverse-sync pending > syncing. Anything that loses stays
        log-file-only (Open log folder), not shown here -- a deliberate
        scope reduction from the old up-to-four-simultaneous message
        rows."""
        if snap.rig_error:
            return f"rig error: {snap.rig_error}", "disconnected"
        if snap.websdr_active and snap.websdr is not None and snap.websdr.last_error:
            return f"WebSDR error: {snap.websdr.last_error}", "paused"
        if snap.reverse_sync_error:
            return f"reverse sync: {snap.reverse_sync_error}", "paused"

        segments = [self._rig_status_text()]
        if self._state.rig_connected:
            if self._state.paused:
                segments.append(
                    f"sync paused — rig {fmt_hz(self._state.rx_hz)} / sdr {fmt_hz(self._state.sdr_hz)}"
                )
            elif snap.reverse_sync_pending:
                segments.append(f"reverse sync: {snap.reverse_sync_pending}")
            else:
                segments.append("syncing")
        if self._state.sdr_connected and self._state.site:
            segments.append(self._state.site)

        dot_mode = "paused" if self._state.paused else ("syncing" if self._state.rig_connected else "disconnected")
        return " · ".join(segments), dot_mode


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
