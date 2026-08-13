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
    # Mirrors WebSDRStatus.audio_active (sdrsync/websdr/base.py) -- a
    # best-effort, driver-specific proxy (socket/AudioContext state, not
    # actual sample amplitude) for "the WebSDR's own audio is flowing",
    # None when unknown (driver didn't report / not connected yet).
    sdr_audio_active: Optional[bool] = None
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
    # spec §9 (compact bar/undock) -- True whenever MainFrame is hidden
    # in favour of CompactFrame. Set directly by MainFrame's
    # _on_undock_clicked/_on_dock_clicked (there's no StatusSnapshot
    # field for it -- this is pure GUI-side chrome state, not something
    # the engine has any notion of).
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


def ptt_tag_state(state: AppState) -> str:
    """Shared by strip_panel/compact_frame's _PttTag.set_state() calls.

    "receive" requires more than the rig being connected: the WebSDR page
    itself must be connected too, and -- when the driver reports it --
    its audio must actually be active. Without this, RECEIVE showed as
    soon as the rig connected even with no WebSDR site loaded at all."""
    if not state.rig_connected:
        return "offline"
    if state.ptt:
        return "transmit"
    if state.sdr_connected and state.sdr_audio_active is not False:
        return "receive"
    return "idle"
