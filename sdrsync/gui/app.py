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
import queue
import threading
from typing import Callable, Optional

import wx

from sdrsync import __version__
from sdrsync.browser.backend import EdgeBackendUnavailable, assert_edge_available, ensure_webview_backend
from sdrsync.config import MAX_POLL_INTERVAL_S, MIN_POLL_INTERVAL_S, AppSettings, KNOWN_SITES, WebSDRSite
from sdrsync.gui_messages import GuiMessage
from sdrsync.gui.webview_host import WebViewHost
from sdrsync.preflight import (
    DetectResult,
    RigPreflightResult,
    WebsdrPreflightResult,
    check_rigctld,
    check_websdr_url,
    detect_websdr_type,
)
from sdrsync.sync.engine import StatusSnapshot, SyncEngine

logger = logging.getLogger("sdrsync.gui")

QUEUE_POLL_MS = 150
SHUTDOWN_TIMEOUT_S = 5.0
CUSTOM_URL_SENTINEL = "Custom URL..."
LABEL_WRAP_PX = 380
ERROR_COLOUR = wx.Colour(200, 0, 0)


def _set_wrapped(static_text: wx.StaticText, text: str, width: int = LABEL_WRAP_PX) -> None:
    static_text.SetLabel(text)
    static_text.Wrap(width)


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


class MainFrame(wx.Frame):
    def __init__(self, settings: AppSettings, webview_host: WebViewHost) -> None:
        super().__init__(None, title="SDRSync - rigctld -> WebSDR")
        self.settings = settings
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

        # Mirrors of the engine's independent subsystem states, updated
        # from each StatusSnapshot -- drive button labels/enabled-state and
        # "connecting.../reconnecting..." status wording (see
        # _update_websdr_controls / _apply_snapshot).
        self._rig_active = False
        self._rig_ever_connected = False
        self._websdr_active = False
        self._websdr_ever_connected = False
        # What the WebSDR panel last told the engine to load -- None means
        # "not active". Used to tell "Disconnect" (same site reselected)
        # apart from "Switch WebSDR" (different site selected while active).
        self._active_websdr_site: Optional[WebSDRSite] = None
        # Mirrors the panel's current status text -- used the same way the
        # Tkinter version read the conn_var StringVar back for gating logic.
        self._websdr_conn_text = "not connected"

        self._dispatch: dict[type, Callable[[GuiMessage], None]] = {
            StatusSnapshot: self._apply_snapshot,
            RigPreflightResult: self._apply_rig_preflight,
            WebsdrPreflightResult: self._apply_websdr_preflight,
            DetectResult: self._apply_detect_result,
        }

        self.Bind(wx.EVT_CLOSE, self._on_close)

        self._build_widgets()
        self._restore_custom_site_if_needed()

        # One engine, one background thread, for the whole app session --
        # NOT recreated per Connect click. The rig/WebSDR subsystems it
        # owns are started/stopped independently via the buttons below.
        self.engine = SyncEngine(self.settings, self.status_queue, webview_host=self._webview_host)
        self.thread = threading.Thread(target=self._run_engine, args=(self.engine,), daemon=True)
        self.thread.start()

        self._poll_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._poll_status_queue, self._poll_timer)
        self._poll_timer.Start(QUEUE_POLL_MS)

    @staticmethod
    def _run_engine(engine: SyncEngine) -> None:
        try:
            asyncio.run(engine.run())
        except Exception as e:
            logger.exception("Sync engine crashed")
            engine.publish_fatal_error(_describe_engine_crash(e))

    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        panel = wx.Panel(self)
        self._root_panel = panel
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        websdr_box = wx.StaticBox(panel, label="WebSDR")
        websdr_sizer = wx.StaticBoxSizer(websdr_box, wx.VERTICAL)
        self._build_websdr_panel(websdr_box, websdr_sizer)
        main_sizer.Add(websdr_sizer, flag=wx.EXPAND | wx.ALL, border=6)

        rig_box = wx.StaticBox(panel, label="Transceiver (rigctld)")
        rig_sizer = wx.StaticBoxSizer(rig_box, wx.VERTICAL)
        self._build_rig_panel(rig_box, rig_sizer)
        main_sizer.Add(rig_sizer, flag=wx.EXPAND | wx.ALL, border=6)

        self._build_mock_rig_panel(panel, main_sizer)

        panel.SetSizer(main_sizer)
        frame_sizer = wx.BoxSizer(wx.VERTICAL)
        frame_sizer.Add(panel, flag=wx.EXPAND)
        self.SetSizerAndFit(frame_sizer)

    def _build_websdr_panel(self, parent: wx.Window, outer: wx.BoxSizer) -> None:
        grid = wx.GridBagSizer(vgap=4, hgap=6)
        row = 0

        grid.Add(wx.StaticText(parent, label="WebSDR site:"), pos=(row, 0), flag=wx.ALIGN_CENTER_VERTICAL)
        selected_site = self._find_any_site_by_url(self.settings.last_site_url) or KNOWN_SITES[0]
        self.site_combo = wx.ComboBox(parent, value=selected_site.name, choices=[], style=wx.CB_READONLY)
        self.site_combo.Bind(wx.EVT_COMBOBOX, self._on_site_selected)
        grid.Add(self.site_combo, pos=(row, 1), span=(1, 2), flag=wx.EXPAND)

        self.websdr_connect_btn = wx.Button(parent, label="Connect")
        self.websdr_connect_btn.Bind(wx.EVT_BUTTON, self._on_websdr_connect_clicked)
        _Tooltip(self.websdr_connect_btn, self._websdr_connect_tooltip_text)
        grid.Add(self.websdr_connect_btn, pos=(row, 3), flag=wx.EXPAND)

        self.delete_site_btn = wx.Button(parent, label="Delete")
        self.delete_site_btn.Bind(wx.EVT_BUTTON, self._on_delete_site_clicked)
        _Tooltip(self.delete_site_btn, self._delete_site_tooltip_text)
        grid.Add(self.delete_site_btn, pos=(row, 4), flag=wx.EXPAND)
        self._refresh_site_dropdown_values()
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

        self.websdr_preflight_text = wx.StaticText(parent, label="", size=(LABEL_WRAP_PX, -1))
        grid.Add(self.websdr_preflight_text, pos=(row, 0), span=(1, 4), flag=wx.EXPAND)
        row += 1

        self.websdr_conn_text = wx.StaticText(parent, label="not connected")
        self.websdr_freq_text = wx.StaticText(parent, label="-")
        self.websdr_mode_text = wx.StaticText(parent, label="-")
        self.websdr_audio_text = wx.StaticText(parent, label="-")
        row = self._grid_status_row(parent, grid, row, "Status:", self.websdr_conn_text)
        row = self._grid_status_row(parent, grid, row, "Frequency:", self.websdr_freq_text)
        row = self._grid_status_row(parent, grid, row, "Mode:", self.websdr_mode_text)
        row = self._grid_status_row(parent, grid, row, "Audio:", self.websdr_audio_text)

        self.websdr_err_text = wx.StaticText(parent, label="", size=(LABEL_WRAP_PX, -1))
        self.websdr_err_text.SetForegroundColour(ERROR_COLOUR)
        grid.Add(self.websdr_err_text, pos=(row, 0), span=(1, 4), flag=wx.EXPAND)

        grid.AddGrowableCol(1)
        outer.Add(grid, flag=wx.EXPAND | wx.ALL, border=4)
        self._update_websdr_controls()

    def _build_rig_panel(self, parent: wx.Window, outer: wx.BoxSizer) -> None:
        grid = wx.GridBagSizer(vgap=4, hgap=6)
        row = 0

        grid.Add(wx.StaticText(parent, label="rigctld host:"), pos=(row, 0), flag=wx.ALIGN_CENTER_VERTICAL)
        self.host_entry = wx.TextCtrl(parent, value=self.settings.rigctld_host, size=(140, -1))
        grid.Add(self.host_entry, pos=(row, 1), flag=wx.ALIGN_CENTER_VERTICAL)
        grid.Add(wx.StaticText(parent, label="port:"), pos=(row, 2),
                 flag=wx.ALIGN_CENTER_VERTICAL | wx.ALIGN_RIGHT)
        self.port_entry = wx.TextCtrl(parent, value=str(self.settings.rigctld_port), size=(80, -1))
        grid.Add(self.port_entry, pos=(row, 3), flag=wx.ALIGN_CENTER_VERTICAL)
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

        self.rig_preflight_text = wx.StaticText(parent, label="", size=(LABEL_WRAP_PX, -1))
        grid.Add(self.rig_preflight_text, pos=(row, 0), span=(1, 4), flag=wx.EXPAND)
        row += 1

        self.rig_conn_text = wx.StaticText(parent, label="not connected")
        self.rig_freq_text = wx.StaticText(parent, label="-")
        self.rig_mode_text = wx.StaticText(parent, label="-")
        self.rig_ptt_text = wx.StaticText(parent, label="-")
        row = self._grid_status_row(parent, grid, row, "Status:", self.rig_conn_text)
        row = self._grid_status_row(parent, grid, row, "Frequency:", self.rig_freq_text)
        row = self._grid_status_row(parent, grid, row, "Mode:", self.rig_mode_text)
        row = self._grid_status_row(parent, grid, row, "PTT:", self.rig_ptt_text)

        self.rig_err_text = wx.StaticText(parent, label="", size=(LABEL_WRAP_PX, -1))
        self.rig_err_text.SetForegroundColour(ERROR_COLOUR)
        grid.Add(self.rig_err_text, pos=(row, 0), span=(1, 4), flag=wx.EXPAND)

        grid.AddGrowableCol(1)
        outer.Add(grid, flag=wx.EXPAND | wx.ALL, border=4)

        if self.mock_rig_check.GetValue():
            self.host_entry.Disable()

    def _build_mock_rig_panel(self, parent: wx.Window, outer_sizer: wx.BoxSizer) -> None:
        """Only ever shown while 'Use mock rig' is checked AND the rig
        subsystem is connected -- a real-rig session must never show
        controls that look like they drive a real radio."""
        self.mock_box = wx.StaticBox(parent, label="Mock Rig Control")
        mock_sizer = wx.StaticBoxSizer(self.mock_box, wx.VERTICAL)
        grid = wx.GridBagSizer(vgap=4, hgap=6)

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

        mock_sizer.Add(grid, flag=wx.EXPAND | wx.ALL, border=4)
        self.mock_err_text = wx.StaticText(self.mock_box, label="")
        self.mock_err_text.SetForegroundColour(ERROR_COLOUR)
        mock_sizer.Add(self.mock_err_text, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=4)

        outer_sizer.Add(mock_sizer, flag=wx.EXPAND | wx.ALL, border=6)
        self.mock_box.Show(False)  # hidden until mock mode + rig connected

    @staticmethod
    def _grid_status_row(parent: wx.Window, grid: wx.GridBagSizer, row: int, label: str, value_widget: wx.StaticText) -> int:
        grid.Add(wx.StaticText(parent, label=label), pos=(row, 0), flag=wx.ALIGN_CENTER_VERTICAL)
        grid.Add(value_widget, pos=(row, 1), flag=wx.ALIGN_CENTER_VERTICAL)
        return row + 1

    # ------------------------------------------------------------------ WebSDR panel
    def _on_site_selected(self, _event=None) -> None:
        # A Test result (or a stale error already shown from a previous
        # site) describes whatever was selected when it ran -- leaving it
        # on screen after switching sites is misleading, since it reads as
        # if it still applies to the newly selected one.
        self.websdr_preflight_text.SetLabel("")
        self._update_websdr_controls()

    def _on_custom_url_edited(self, _event=None) -> None:
        self.websdr_preflight_text.SetLabel("")
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
            and self._websdr_conn_text == "connected"
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
            # cover") -- require the rig connected first.
            self.websdr_connect_btn.SetLabel("Connect")
            self.websdr_connect_btn.Enable(self._rig_active)
            self.headless_check.Enable(True)
            return

        self.headless_check.Enable(False)
        selected = self._resolve_selected_site()
        active = self._active_websdr_site
        if (
            selected is not None and active is not None
            and selected.url == active.url and selected.driver_type == active.driver_type
        ):
            self.websdr_connect_btn.SetLabel("Disconnect")
        else:
            self.websdr_connect_btn.SetLabel("Switch WebSDR")
        self.websdr_connect_btn.Enable(True)

    def _websdr_connect_tooltip_text(self) -> Optional[str]:
        # The only reason Connect is ever disabled (as opposed to clickable
        # but rejected with an error, e.g. an undetected Custom URL) is the
        # rig-first gate -- explain that on hover rather than leaving a
        # greyed-out button with no clue why.
        if not self.websdr_connect_btn.IsEnabled() and not self._rig_active:
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
        return next((s for s in KNOWN_SITES + self._user_sites if s.name == name), None)

    def _find_any_site_by_url(self, url: str) -> Optional[WebSDRSite]:
        return next((s for s in KNOWN_SITES + self._user_sites if s.url == url), None)

    def _site_already_saved(self, site: WebSDRSite) -> bool:
        return self._find_any_site_by_url(site.url) is not None

    def _refresh_site_dropdown_values(self) -> None:
        names = [s.name for s in KNOWN_SITES + self._user_sites] + [CUSTOM_URL_SENTINEL]
        current = self.site_combo.GetValue()
        self.site_combo.Set(names)
        if current in names:
            self.site_combo.SetValue(current)

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

    def _restore_custom_site_if_needed(self) -> None:
        if self._find_any_site_by_url(self.settings.last_site_url) is not None:
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
            self.websdr_preflight_text.SetLabel("Select a known site, or Detect a Custom URL first")
            return
        self.websdr_test_btn.Enable(False)
        self.websdr_test_btn.SetLabel("Testing...")
        self.websdr_preflight_text.SetLabel("Checking WebSDR reachability...")
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
        self.websdr_preflight_text.SetLabel(("OK: " if result.ok else "FAIL: ") + result.message)

    def _on_websdr_connect_clicked(self, _event=None) -> None:
        active = self._active_websdr_site
        if not self._websdr_active and not self._rig_active:
            # Belt-and-suspenders -- the button is disabled in this state
            # (see _update_websdr_controls()), but guard the handler too in
            # case of a race between a click and a state-changing snapshot.
            self.websdr_err_text.SetLabel("Connect the transceiver first")
            return

        site = self._resolve_selected_site()
        if site is None:
            self.websdr_err_text.SetLabel("Select a known site, or Detect a Custom URL first")
            return

        if self._websdr_active and active is not None and site.url == active.url and site.driver_type == active.driver_type:
            self.websdr_connect_btn.Enable(False)
            self.websdr_connect_btn.SetLabel("Disconnecting...")
            self.engine.stop_websdr_from_other_thread()
            return

        # Connect (not active) or Switch (active, different site) -- same
        # call either way; the engine replaces whatever's currently loaded.
        self.settings.last_site_url = site.url
        self.settings.last_site_driver_type = site.driver_type if self._find_any_site_by_url(site.url) is None else ""
        self.settings.headless = self.headless_check.GetValue()
        self.settings.save()
        self._webview_host.set_headless(self.settings.headless)

        self._websdr_ever_connected = False
        self._active_websdr_site = site
        self._websdr_conn_text = "connecting..."
        self.websdr_conn_text.SetLabel("connecting...")
        self.websdr_freq_text.SetLabel("-")
        self.websdr_mode_text.SetLabel("-")
        self.websdr_audio_text.SetLabel("-")
        self.websdr_err_text.SetLabel("")
        self.websdr_connect_btn.Enable(False)
        self.websdr_connect_btn.SetLabel("Connecting...")
        self.engine.start_websdr_from_other_thread(site)

    # ------------------------------------------------------------------ Transceiver panel
    def _on_mock_rig_toggled(self, _event=None) -> None:
        is_mock = self.mock_rig_check.GetValue()
        self.host_entry.Enable(not is_mock)

    def _on_poll_interval_changed(self, _event=None) -> None:
        # SyncEngine reads self.settings.poll_interval_s live on every tick
        # (sync/engine.py's _poll_loop), so this applies immediately --
        # including mid-session -- with no reconnect needed.
        self.settings.poll_interval_s = self.poll_interval_ctrl.GetValue()
        self.settings.save()

    def _update_mock_rig_panel_visibility(self) -> None:
        # Gate on the *running rig session's* mode, not the live checkbox
        # -- the checkbox is disabled while the rig is connected so they
        # can't diverge, but this is the real invariant: a real-rig session
        # must never show controls that look like they drive a real radio,
        # even transiently.
        should_show = self._rig_active and self.settings.use_mock_rig
        if self.mock_box.IsShown() != should_show:
            self.mock_box.Show(should_show)
            self._root_panel.Layout()
            self.Fit()

    def _on_mock_set_freq(self, _event=None) -> None:
        try:
            freq_hz = int(self.mock_freq_entry.GetValue())
        except ValueError:
            self.mock_err_text.SetLabel("Frequency must be a whole number of Hz")
            return
        self.mock_err_text.SetLabel("")
        self.engine.push_mock_freq(freq_hz)

    def _on_mock_set_mode(self, _event=None) -> None:
        try:
            passband_hz = int(self.mock_passband_entry.GetValue())
        except ValueError:
            self.mock_err_text.SetLabel("Passband must be a whole number of Hz")
            return
        self.mock_err_text.SetLabel("")
        self.engine.push_mock_mode(self.mock_mode_combo.GetValue(), passband_hz)

    def _on_mock_ptt_toggled(self, _event=None) -> None:
        self.engine.push_mock_ptt(self.mock_ptt_check.GetValue())

    def _on_rig_test_clicked(self, _event=None) -> None:
        if self.rig_test_thread is not None and self.rig_test_thread.is_alive():
            return
        try:
            port = int(self.port_entry.GetValue())
        except ValueError:
            self.rig_preflight_text.SetLabel("Invalid rigctld port")
            return
        host = self.host_entry.GetValue().strip() or "127.0.0.1"
        self.rig_test_btn.Enable(False)
        self.rig_test_btn.SetLabel("Testing...")
        self.rig_preflight_text.SetLabel("Checking rigctld reachability...")
        self.rig_test_thread = threading.Thread(
            target=self._run_rig_test, args=(host, port, self.status_queue), daemon=True
        )
        self.rig_test_thread.start()

    @staticmethod
    def _run_rig_test(host: str, port: int, status_queue: "queue.Queue") -> None:
        try:
            ok, message = asyncio.run(check_rigctld(host, port))
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
        self.rig_preflight_text.SetLabel(("OK: " if result.ok else "FAIL: ") + result.message)

    def _on_rig_connect_clicked(self, _event=None) -> None:
        if self._rig_active:
            self.rig_connect_btn.Enable(False)
            self.rig_connect_btn.SetLabel("Disconnecting...")
            self.engine.stop_rig_from_other_thread()
            return

        try:
            port = int(self.port_entry.GetValue())
        except ValueError:
            self.rig_err_text.SetLabel("Invalid rigctld port")
            return

        use_mock = self.mock_rig_check.GetValue()
        # A mock rig only ever exists on loopback -- ignore whatever's typed
        # in the host field rather than silently trying to bind a mock
        # server to some other address.
        host = "127.0.0.1" if use_mock else (self.host_entry.GetValue().strip() or "127.0.0.1")

        self.settings.rigctld_host = host
        self.settings.rigctld_port = port
        self.settings.use_mock_rig = use_mock
        self.settings.save()

        self._rig_ever_connected = False
        self.rig_conn_text.SetLabel("connecting...")
        self.rig_err_text.SetLabel("")
        self.rig_connect_btn.Enable(False)
        self.rig_connect_btn.SetLabel("Connecting...")
        self.mock_rig_check.Enable(False)
        self.host_entry.Enable(False)
        self.engine.start_rig_from_other_thread(host, port, use_mock)

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

    def _apply_snapshot(self, snap: StatusSnapshot) -> None:
        if snap.fatal_error:
            # The whole background thread has died -- both subsystems are
            # gone with it and there's nothing to salvage short of
            # restarting the app. Distinct from (and far rarer than) either
            # subsystem's own start failing, which is reported through its
            # own error field further down instead.
            _set_wrapped(self.websdr_err_text, f"Sync engine crashed: {snap.fatal_error}")
            _set_wrapped(self.rig_err_text, f"Sync engine crashed: {snap.fatal_error}")
            self.websdr_connect_btn.Enable(False)
            self.rig_connect_btn.Enable(False)
            self.websdr_conn_text.SetLabel("error")
            self.rig_conn_text.SetLabel("error")
            return

        # --- Transceiver ---
        self._rig_active = snap.rig_active
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
            self.mock_rig_check.Enable(True)
        _set_wrapped(self.rig_err_text, snap.rig_error or "")
        self._update_mock_rig_panel_visibility()

        # --- WebSDR ---
        self._websdr_active = snap.websdr_active
        if not snap.websdr_active:
            self._active_websdr_site = None
            self._websdr_conn_text = "not connected"
            self.websdr_conn_text.SetLabel("not connected")
            self.websdr_freq_text.SetLabel("-")
            self.websdr_mode_text.SetLabel("-")
            self.websdr_audio_text.SetLabel("-")
            _set_wrapped(self.websdr_err_text, snap.websdr.last_error if snap.websdr is not None else "")
        else:
            ws = snap.websdr
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
                _set_wrapped(self.websdr_err_text, ws.last_error or "")
        self._update_websdr_controls()

    # ------------------------------------------------------------------
    def _on_close(self, _event=None) -> None:
        self.engine.stop_from_other_thread()
        self.thread.join(timeout=SHUTDOWN_TIMEOUT_S)
        self._poll_timer.Stop()
        # The WebViewHost's frame is separate from this one and would
        # otherwise keep the whole app alive after this window closes
        # (wx's default "exit when no top-level windows exist" behavior
        # only triggers once the LAST one closes) -- destroy it explicitly.
        try:
            self._webview_host.frame.Destroy()
        except Exception:
            pass
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
            assert_edge_available()
        except EdgeBackendUnavailable as e:
            wx.MessageBox(str(e), "SDRSync cannot start", wx.OK | wx.ICON_ERROR)
            return False

        settings = AppSettings.load()
        host = WebViewHost(headless=settings.headless)
        frame = MainFrame(settings, host)
        self.SetTopWindow(frame)
        frame.Show(True)
        return True


def main() -> None:
    app = SDRSyncApp(False)
    app.MainLoop()


if __name__ == "__main__":
    main()
