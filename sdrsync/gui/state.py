"""AppState (spec §10) -- one dataclass every chrome band reads from via
its own refresh(state) method. Population from real StatusSnapshot/
AppSettings data lands in a later phase (build_app_state()); until then
MainFrame owns one mutable instance and updates fields directly from its
own (temporary, pre-engine) click handlers -- see project_brief.md's GUI
rewrite progress log for which phase is current.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class AppState:
    rig_connected: bool = False
    sdr_connected: bool = False
    # Not in spec §10's literal field list -- a real engineering
    # necessity found during phase 6 verification: True from the moment
    # a WebSDR connect/switch is requested, before the page confirms
    # connected. ReceiverHost needs this (not sdr_connected) to decide
    # when to show ReceiverLive, because the wx.html2.WebView widget
    # must already be part of the visible window tree (nonzero size, a
    # real HWND) for WebView2 to initialize correctly -- confirmed live:
    # creating it while ReceiverLive was still hidden (waiting for
    # sdr_connected) produced "Invalid window handle"/DPI-query errors
    # from wx's own MSW backend. Mirrors the rig_active/rig_connected
    # distinction the rest of the app already uses for the same reason.
    sdr_active: bool = False
    # Always False this rewrite -- spec §9 (compact bar/undock) is out
    # of scope; kept as a real field so panels can already read it the
    # way they will once undock exists.
    undocked: bool = False
    open_panel: Optional[str] = None  # "transceiver" | "sites" | "behaviour" | None
    rx_hz: int = 0
    tx_hz: int = 0  # mirrors rx_hz -- no real split/second-VFO polling this rewrite
    sdr_hz: int = 0
    mode: str = "USB"
    ptt: bool = False
    paused: bool = False  # session-only, engine-mirrored; never persisted
    mute_on_tx: bool = True
    sync_tx_vfo: bool = True
    mock_rig: bool = False
    site: str = ""
