"""Basic Tkinter control panel.

Thread contract:
    - Tkinter owns the main thread.
    - The asyncio stack (Playwright + rigctld + SyncEngine) runs in one
      background thread, for the whole app session (started once at
      startup, joined once when the window closes) via
      asyncio.run(engine.run()).
    - Status flows background -> GUI only through a queue.Queue, drained by
      root.after() on the Tk thread. No Tk widget is ever touched from the
      background thread.
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
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from sdrsync.config import AppSettings, KNOWN_SITES, WebSDRSite, find_site_by_url
from sdrsync.gui_messages import GuiMessage
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


class App:
    def __init__(self, root: tk.Tk, settings: AppSettings) -> None:
        self.root = root
        self.settings = settings

        self.status_queue: "queue.Queue[GuiMessage]" = queue.Queue()
        self.rig_test_thread: Optional[threading.Thread] = None
        self.websdr_test_thread: Optional[threading.Thread] = None
        self.detect_thread: Optional[threading.Thread] = None

        # Set on a successful Detect click; the actual WebSDRSite to connect
        # to when the Custom URL sentinel is selected. None until detected.
        self._custom_site: Optional[WebSDRSite] = None

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

        self._dispatch: dict[type, Callable[[GuiMessage], None]] = {
            StatusSnapshot: self._apply_snapshot,
            RigPreflightResult: self._apply_rig_preflight,
            WebsdrPreflightResult: self._apply_websdr_preflight,
            DetectResult: self._apply_detect_result,
        }

        root.title("SDRSync - rigctld -> WebSDR")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_widgets()
        self._restore_custom_site_if_needed()

        # One engine, one background thread, for the whole app session --
        # NOT recreated per Connect click. The rig/WebSDR subsystems it
        # owns are started/stopped independently via the buttons below.
        self.engine = SyncEngine(self.settings, self.status_queue)
        self.thread = threading.Thread(target=self._run_engine, args=(self.engine,), daemon=True)
        self.thread.start()

        self.root.after(QUEUE_POLL_MS, self._poll_status_queue)

    @staticmethod
    def _run_engine(engine: SyncEngine) -> None:
        try:
            asyncio.run(engine.run())
        except Exception as e:
            logger.exception("Sync engine crashed")
            engine.publish_fatal_error(_describe_engine_crash(e))

    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        pad = {"padx": 6, "pady": 4}

        websdr_frame = ttk.LabelFrame(self.root, text="WebSDR")
        websdr_frame.grid(row=0, column=0, sticky="ew", **pad)
        self._build_websdr_panel(websdr_frame)

        rig_frame = ttk.LabelFrame(self.root, text="Transceiver (rigctld)")
        rig_frame.grid(row=1, column=0, sticky="ew", **pad)
        self._build_rig_panel(rig_frame)

        self._build_mock_rig_panel(pad)

        for frame in (websdr_frame, rig_frame):
            frame.grid_columnconfigure(1, weight=1)

    def _build_websdr_panel(self, f: ttk.Frame) -> None:
        ttk.Label(f, text="WebSDR site:").grid(row=0, column=0, sticky="w")
        self.site_var = tk.StringVar()
        site_names = [s.name for s in KNOWN_SITES] + [CUSTOM_URL_SENTINEL]
        selected_site = find_site_by_url(self.settings.last_site_url) or KNOWN_SITES[0]
        self.site_var.set(selected_site.name)
        self.site_combo = ttk.Combobox(f, textvariable=self.site_var, values=site_names, state="readonly", width=30)
        self.site_combo.grid(row=0, column=1, columnspan=2, sticky="ew")
        self.site_combo.bind("<<ComboboxSelected>>", self._on_site_selected)

        self.websdr_connect_btn = ttk.Button(f, text="Connect", command=self._on_websdr_connect_clicked)
        self.websdr_connect_btn.grid(row=0, column=3, sticky="ew")

        ttk.Label(f, text="Custom URL:").grid(row=1, column=0, sticky="w")
        self.custom_url_var = tk.StringVar(value="")
        self.custom_url_entry = ttk.Entry(f, textvariable=self.custom_url_var, width=32)
        self.custom_url_entry.grid(row=1, column=1, columnspan=2, sticky="ew")
        self.detect_btn = ttk.Button(f, text="Detect", command=self._on_detect_clicked)
        self.detect_btn.grid(row=1, column=3, sticky="ew")

        self.detect_result_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.detect_result_var, wraplength=380).grid(
            row=2, column=0, columnspan=4, sticky="w"
        )

        self.headless_var = tk.BooleanVar(value=self.settings.headless)
        self.headless_check = ttk.Checkbutton(
            f, text="Hide browser window (audio still plays)", variable=self.headless_var
        )
        self.headless_check.grid(row=3, column=0, columnspan=3, sticky="w")

        self.websdr_test_btn = ttk.Button(f, text="Test", command=self._on_websdr_test_clicked)
        self.websdr_test_btn.grid(row=3, column=3, sticky="ew")

        self.websdr_preflight_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.websdr_preflight_var, wraplength=380).grid(
            row=4, column=0, columnspan=4, sticky="w"
        )

        self.websdr_conn_var = tk.StringVar(value="not connected")
        self.websdr_freq_var = tk.StringVar(value="-")
        self.websdr_mode_var = tk.StringVar(value="-")
        self.websdr_audio_var = tk.StringVar(value="-")
        self.websdr_err_var = tk.StringVar(value="")
        self._grid_row(f, 5, "Status:", self.websdr_conn_var)
        self._grid_row(f, 6, "Frequency:", self.websdr_freq_var)
        self._grid_row(f, 7, "Mode:", self.websdr_mode_var)
        self._grid_row(f, 8, "Audio:", self.websdr_audio_var)
        ttk.Label(f, textvariable=self.websdr_err_var, foreground="red", wraplength=380).grid(
            row=9, column=0, columnspan=4, sticky="w"
        )

        self._update_websdr_controls()

    def _build_rig_panel(self, f: ttk.Frame) -> None:
        ttk.Label(f, text="rigctld host:").grid(row=0, column=0, sticky="w")
        self.host_var = tk.StringVar(value=self.settings.rigctld_host)
        self.host_entry = ttk.Entry(f, textvariable=self.host_var, width=16)
        self.host_entry.grid(row=0, column=1, sticky="w")

        ttk.Label(f, text="port:").grid(row=0, column=2, sticky="e")
        self.port_var = tk.StringVar(value=str(self.settings.rigctld_port))
        ttk.Entry(f, textvariable=self.port_var, width=8).grid(row=0, column=3, sticky="w")

        self.mock_rig_var = tk.BooleanVar(value=self.settings.use_mock_rig)
        self.mock_rig_check = ttk.Checkbutton(
            f, text="Use mock rig (embedded, for testing)", variable=self.mock_rig_var,
            command=self._on_mock_rig_toggled,
        )
        self.mock_rig_check.grid(row=1, column=0, columnspan=4, sticky="w")

        self.rig_connect_btn = ttk.Button(f, text="Connect", command=self._on_rig_connect_clicked)
        self.rig_connect_btn.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.rig_test_btn = ttk.Button(f, text="Test", command=self._on_rig_test_clicked)
        self.rig_test_btn.grid(row=2, column=3, sticky="ew", pady=(6, 0))

        self.rig_preflight_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.rig_preflight_var, wraplength=380).grid(
            row=3, column=0, columnspan=4, sticky="w"
        )

        self.rig_conn_var = tk.StringVar(value="not connected")
        self.rig_freq_var = tk.StringVar(value="-")
        self.rig_mode_var = tk.StringVar(value="-")
        self.rig_ptt_var = tk.StringVar(value="-")
        self.rig_err_var = tk.StringVar(value="")
        self._grid_row(f, 4, "Status:", self.rig_conn_var)
        self._grid_row(f, 5, "Frequency:", self.rig_freq_var)
        self._grid_row(f, 6, "Mode:", self.rig_mode_var)
        self._grid_row(f, 7, "PTT:", self.rig_ptt_var)
        ttk.Label(f, textvariable=self.rig_err_var, foreground="red", wraplength=380).grid(
            row=8, column=0, columnspan=4, sticky="w"
        )

        if self.mock_rig_var.get():
            self.host_entry.configure(state="disabled")

    def _build_mock_rig_panel(self, pad: dict) -> None:
        """Only ever shown while 'Use mock rig' is checked AND the rig
        subsystem is connected -- a real-rig session must never show
        controls that look like they drive a real radio."""
        self.mock_frame = ttk.LabelFrame(self.root, text="Mock Rig Control")
        self.mock_frame.grid(row=2, column=0, sticky="ew", **pad)

        self.mock_freq_var = tk.StringVar(value="14074000")
        ttk.Label(self.mock_frame, text="Freq (Hz):").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.mock_frame, textvariable=self.mock_freq_var, width=12).grid(row=0, column=1, sticky="w")
        ttk.Button(self.mock_frame, text="Set Freq", command=self._on_mock_set_freq).grid(row=0, column=2, sticky="w")

        self.mock_mode_var = tk.StringVar(value="USB")
        ttk.Label(self.mock_frame, text="Mode:").grid(row=1, column=0, sticky="w")
        ttk.Combobox(
            self.mock_frame, textvariable=self.mock_mode_var,
            values=["USB", "LSB", "CW", "AM", "FM"], state="readonly", width=8,
        ).grid(row=1, column=1, sticky="w")

        self.mock_passband_var = tk.StringVar(value="2400")
        ttk.Label(self.mock_frame, text="Passband (Hz):").grid(row=2, column=0, sticky="w")
        ttk.Entry(self.mock_frame, textvariable=self.mock_passband_var, width=12).grid(row=2, column=1, sticky="w")
        ttk.Button(self.mock_frame, text="Set Mode", command=self._on_mock_set_mode).grid(row=2, column=2, sticky="w")

        self.mock_ptt_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self.mock_frame, text="PTT (transmitting)", variable=self.mock_ptt_var,
            command=self._on_mock_ptt_toggled,
        ).grid(row=3, column=0, columnspan=2, sticky="w")

        self.mock_err_var = tk.StringVar(value="")
        ttk.Label(self.mock_frame, textvariable=self.mock_err_var, foreground="red").grid(
            row=4, column=0, columnspan=3, sticky="w"
        )

        self.mock_frame.grid_remove()  # hidden until mock mode + rig connected

    @staticmethod
    def _grid_row(frame: ttk.Frame, row: int, label: str, var: tk.StringVar) -> None:
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w")
        ttk.Label(frame, textvariable=var).grid(row=row, column=1, sticky="w")

    # ------------------------------------------------------------------ WebSDR panel
    def _on_site_selected(self, _event=None) -> None:
        self._update_websdr_controls()

    def _update_websdr_controls(self) -> None:
        is_custom = self.site_var.get() == CUSTOM_URL_SENTINEL
        state = "normal" if is_custom else "disabled"
        self.custom_url_entry.configure(state=state)
        self.detect_btn.configure(state=state)

        if not self._websdr_active:
            self.websdr_connect_btn.configure(text="Connect", state="normal")
            self.headless_check.configure(state="normal")
            return

        self.headless_check.configure(state="disabled")
        selected = self._resolve_selected_site()
        active = self._active_websdr_site
        if (
            selected is not None and active is not None
            and selected.url == active.url and selected.driver_type == active.driver_type
        ):
            self.websdr_connect_btn.configure(text="Disconnect", state="normal")
        else:
            self.websdr_connect_btn.configure(text="Switch WebSDR", state="normal")

    def _resolve_selected_site(self) -> Optional[WebSDRSite]:
        """Returns the WebSDRSite the WebSDR panel's controls currently
        point at, or None if the selection is invalid (unrecognized name,
        or Custom URL selected but not yet successfully detected). Callers
        must show an error and refuse to proceed on None -- never silently
        fall back to a different site than what's selected."""
        name = self.site_var.get()
        if name == CUSTOM_URL_SENTINEL:
            if self._custom_site is not None and self._custom_site.url == self.custom_url_var.get().strip():
                return self._custom_site
            return None
        return next((s for s in KNOWN_SITES if s.name == name), None)

    def _restore_custom_site_if_needed(self) -> None:
        if find_site_by_url(self.settings.last_site_url) is not None:
            return
        if not self.settings.last_site_driver_type or not self.settings.last_site_url:
            return
        self._custom_site = WebSDRSite(
            name=f"Custom ({self.settings.last_site_driver_type}): {self.settings.last_site_url}",
            url=self.settings.last_site_url,
            driver_type=self.settings.last_site_driver_type,
        )
        self.site_var.set(CUSTOM_URL_SENTINEL)
        self.custom_url_var.set(self.settings.last_site_url)
        self.detect_result_var.set(f"Restored: {self.settings.last_site_driver_type}")
        self._update_websdr_controls()

    def _on_detect_clicked(self) -> None:
        if self.detect_thread is not None and self.detect_thread.is_alive():
            return  # a detect is already in flight; ignore double-clicks
        url = self.custom_url_var.get().strip()
        if not url:
            self.detect_result_var.set("Enter a URL first")
            return
        self._custom_site = None
        self.detect_btn.configure(state="disabled", text="Detecting...")
        self.detect_result_var.set(f"Detecting WebSDR type at {url}...")
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
        self.detect_btn.configure(text="Detect")
        if not self._websdr_active:
            self._update_websdr_controls()
        if result.url != self.custom_url_var.get().strip():
            # The URL field was edited (or cleared) while this check was in
            # flight -- this result no longer describes what's in the field,
            # so don't let it set _custom_site. Leave the label as-is rather
            # than showing a stale/misleading message for a URL that's no
            # longer visible.
            return
        self.detect_result_var.set(result.message)
        if result.driver_type is not None:
            self._custom_site = WebSDRSite(
                name=f"Custom ({result.driver_type}): {result.url}", url=result.url, driver_type=result.driver_type,
            )
        else:
            self._custom_site = None
        self._update_websdr_controls()

    def _on_websdr_test_clicked(self) -> None:
        if self.websdr_test_thread is not None and self.websdr_test_thread.is_alive():
            return
        site = self._resolve_selected_site()
        if site is None:
            self.websdr_preflight_var.set("Select a known site, or Detect a Custom URL first")
            return
        self.websdr_test_btn.configure(state="disabled", text="Testing...")
        self.websdr_preflight_var.set("Checking WebSDR reachability...")
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
        self.websdr_test_btn.configure(state="normal", text="Test")
        self.websdr_preflight_var.set(("OK: " if result.ok else "FAIL: ") + result.message)

    def _on_websdr_connect_clicked(self) -> None:
        site = self._resolve_selected_site()
        if site is None:
            self.websdr_err_var.set("Select a known site, or Detect a Custom URL first")
            return

        active = self._active_websdr_site
        if self._websdr_active and active is not None and site.url == active.url and site.driver_type == active.driver_type:
            self.websdr_connect_btn.configure(state="disabled", text="Disconnecting...")
            self.engine.stop_websdr_from_other_thread()
            return

        # Connect (not active) or Switch (active, different site) -- same
        # call either way; the engine replaces whatever's currently loaded.
        self.settings.last_site_url = site.url
        self.settings.last_site_driver_type = site.driver_type if find_site_by_url(site.url) is None else ""
        self.settings.headless = self.headless_var.get()
        self.settings.save()

        self._websdr_ever_connected = False
        self._active_websdr_site = site
        self.websdr_conn_var.set("connecting...")
        self.websdr_freq_var.set("-")
        self.websdr_mode_var.set("-")
        self.websdr_audio_var.set("-")
        self.websdr_err_var.set("")
        self.websdr_connect_btn.configure(state="disabled", text="Connecting...")
        self.engine.start_websdr_from_other_thread(site)

    # ------------------------------------------------------------------ Transceiver panel
    def _on_mock_rig_toggled(self) -> None:
        is_mock = self.mock_rig_var.get()
        self.host_entry.configure(state="disabled" if is_mock else "normal")

    def _update_mock_rig_panel_visibility(self) -> None:
        # Gate on the *running rig session's* mode, not the live checkbox
        # -- the checkbox is disabled while the rig is connected so they
        # can't diverge, but this is the real invariant: a real-rig session
        # must never show controls that look like they drive a real radio,
        # even transiently.
        if self._rig_active and self.settings.use_mock_rig:
            self.mock_frame.grid()
        else:
            self.mock_frame.grid_remove()

    def _on_mock_set_freq(self) -> None:
        try:
            freq_hz = int(self.mock_freq_var.get())
        except ValueError:
            self.mock_err_var.set("Frequency must be a whole number of Hz")
            return
        self.mock_err_var.set("")
        self.engine.push_mock_freq(freq_hz)

    def _on_mock_set_mode(self) -> None:
        try:
            passband_hz = int(self.mock_passband_var.get())
        except ValueError:
            self.mock_err_var.set("Passband must be a whole number of Hz")
            return
        self.mock_err_var.set("")
        self.engine.push_mock_mode(self.mock_mode_var.get(), passband_hz)

    def _on_mock_ptt_toggled(self) -> None:
        self.engine.push_mock_ptt(self.mock_ptt_var.get())

    def _on_rig_test_clicked(self) -> None:
        if self.rig_test_thread is not None and self.rig_test_thread.is_alive():
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            self.rig_preflight_var.set("Invalid rigctld port")
            return
        host = self.host_var.get().strip() or "127.0.0.1"
        self.rig_test_btn.configure(state="disabled", text="Testing...")
        self.rig_preflight_var.set("Checking rigctld reachability...")
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
        self.rig_test_btn.configure(state="normal", text="Test")
        self.rig_preflight_var.set(("OK: " if result.ok else "FAIL: ") + result.message)

    def _on_rig_connect_clicked(self) -> None:
        if self._rig_active:
            self.rig_connect_btn.configure(state="disabled", text="Disconnecting...")
            self.engine.stop_rig_from_other_thread()
            return

        try:
            port = int(self.port_var.get())
        except ValueError:
            self.rig_err_var.set("Invalid rigctld port")
            return

        use_mock = self.mock_rig_var.get()
        # A mock rig only ever exists on loopback -- ignore whatever's typed
        # in the host field rather than silently trying to bind a mock
        # server to some other address.
        host = "127.0.0.1" if use_mock else (self.host_var.get().strip() or "127.0.0.1")

        self.settings.rigctld_host = host
        self.settings.rigctld_port = port
        self.settings.use_mock_rig = use_mock
        self.settings.save()

        self._rig_ever_connected = False
        self.rig_conn_var.set("connecting...")
        self.rig_err_var.set("")
        self.rig_connect_btn.configure(state="disabled", text="Connecting...")
        self.mock_rig_check.configure(state="disabled")
        self.host_entry.configure(state="disabled")
        self.engine.start_rig_from_other_thread(host, port, use_mock)

    # ------------------------------------------------------------------
    def _poll_status_queue(self) -> None:
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
        self.root.after(QUEUE_POLL_MS, self._poll_status_queue)

    def _apply_snapshot(self, snap: StatusSnapshot) -> None:
        if snap.fatal_error:
            # The whole background thread has died (e.g. the Playwright
            # driver process itself crashing) -- both subsystems are gone
            # with it and there's nothing to salvage short of restarting
            # the app. Distinct from (and far rarer than) either
            # subsystem's own start failing, which is reported through its
            # own error field further down instead.
            self.websdr_err_var.set(f"Sync engine crashed: {snap.fatal_error}")
            self.rig_err_var.set(f"Sync engine crashed: {snap.fatal_error}")
            self.websdr_connect_btn.configure(state="disabled")
            self.rig_connect_btn.configure(state="disabled")
            self.websdr_conn_var.set("error")
            self.rig_conn_var.set("error")
            return

        # --- Transceiver ---
        self._rig_active = snap.rig_active
        if snap.rig_active:
            if snap.rig_connected:
                self._rig_ever_connected = True
                self.rig_conn_var.set("connected")
            else:
                self.rig_conn_var.set("reconnecting..." if self._rig_ever_connected else "connecting...")
            self.rig_freq_var.set(f"{snap.rig_freq_hz/1000:.3f} kHz" if snap.rig_freq_hz else "-")
            self.rig_mode_var.set(snap.rig_mode or "-")
            self.rig_ptt_var.set("TX" if snap.rig_ptt else ("RX" if snap.rig_ptt is not None else "-"))
            self.rig_connect_btn.configure(state="normal", text="Disconnect")
        else:
            self.rig_conn_var.set("not connected")
            self.rig_freq_var.set("-")
            self.rig_mode_var.set("-")
            self.rig_ptt_var.set("-")
            self.rig_connect_btn.configure(state="normal", text="Connect")
            self.host_entry.configure(state="disabled" if self.mock_rig_var.get() else "normal")
            self.mock_rig_check.configure(state="normal")
        self.rig_err_var.set(snap.rig_error or "")
        self._update_mock_rig_panel_visibility()

        # --- WebSDR ---
        self._websdr_active = snap.websdr_active
        if not snap.websdr_active:
            self._active_websdr_site = None
            self.websdr_conn_var.set("not connected")
            self.websdr_freq_var.set("-")
            self.websdr_mode_var.set("-")
            self.websdr_audio_var.set("-")
            self.websdr_err_var.set(snap.websdr.last_error if snap.websdr is not None else "")
        else:
            ws = snap.websdr
            if ws is not None:
                if ws.connected:
                    self._websdr_ever_connected = True
                    self.websdr_conn_var.set("connected")
                else:
                    self.websdr_conn_var.set("reconnecting..." if self._websdr_ever_connected else "connecting...")
                self.websdr_freq_var.set(f"{ws.freq_hz/1000:.3f} kHz" if ws.freq_hz else "-")
                self.websdr_mode_var.set(ws.mode or "-")
                if ws.audio_active is None:
                    self.websdr_audio_var.set("-")
                else:
                    self.websdr_audio_var.set("streaming" if ws.audio_active else "silent")
                self.websdr_err_var.set(ws.last_error or "")
        self._update_websdr_controls()

    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        self.engine.stop_from_other_thread()
        self.thread.join(timeout=SHUTDOWN_TIMEOUT_S)
        self.root.destroy()


def _describe_engine_crash(e: Exception) -> str:
    text = str(e)
    if "Executable doesn't exist" in text or "playwright install" in text:
        return (
            "Chromium isn't installed for Playwright yet. Open a terminal and run: "
            "playwright install chromium -- then restart sdrsync."
        )
    return str(e)


def main() -> None:
    from sdrsync.logging_setup import setup_logging

    setup_logging()
    settings = AppSettings.load()
    root = tk.Tk()
    App(root, settings)
    root.mainloop()


if __name__ == "__main__":
    main()
