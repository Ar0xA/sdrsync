"""Driver for the OpenWebRX software family (including the OpenWebRX+ fork
-- same client JS, same driver).

Strategy: same as the other drivers -- drive the real page's own control
objects (confirmed by reading the live site's actual bundled JS and
calling them live in a real browser tab against a real receiver) via
Playwright's page.evaluate, rather than reimplementing the control
WebSocket's JSON protocol.

Confirmed live (against http://sdr2.justjakob.de/, cross-checked for the
fingerprint/structure against two other instances on different versions):
    $('#openwebrx-panel-receiver').demodulatorPanel().getDemodulator()
        -- the one-and-only active demodulator object. Confirmed
        `.set_offset_frequency(offset_hz)` retunes the receiver and
        `.offset_frequency` reads it back; `demodulatorPanel().setMode(mode)`
        changes mode and `.getDemodulator().modulation` reads it back.
    Frequency is OFFSET-RELATIVE, not absolute: offset_hz is relative to
        the top-level global `window.center_freq`, and the page itself
        no-ops a set_offset_frequency() call if abs(offset_hz) exceeds
        window.bandwidth / 2 (some stations run multiple SDR profiles
        with different center/bandwidth; switching profiles to cover an
        out-of-range request is not implemented -- see tune_hz()).
    window.ws -- the control WebSocket; ws.readyState === 1 (OPEN) is the
        reliable "ready" signal, same semantics as KiwiSDR's ws_snd.
    Valid modulation strings come from the server at runtime
        (Modes.getModes()), not hardcoded client JS -- confirmed live:
        nfm/wfm/am/lsb/usb/cw are the analog ones. Unlike the other two
        drivers there is NO narrow/wide suffix convention here, so
        map_hamlib_mode_openwebrx() is a plain 1:1 dict.
    toggleMute() -- confirmed live: toggles a UI class on
        '.openwebrx-mute-button' and drives audioEngine.setVolume(). No
        longer called by sdrsync (v15 moved mute-on-TX to a native,
        page-independent WebView-level mute -- see browser_shim.py's
        set_muted()), kept here as a still-true fact about this site's
        own JS in case it's needed again.
    The demodulator does not exist/start immediately when `ws` opens (the
        page's own init sequence creates it slightly later, gated on
        Modes.initComplete() && center_freq) -- confirmed live via
        getDemodulators()[0].started flipping true only after that. attach()
        must wait for this, the same class of startup race already hit and
        fixed for KiwiSDR (there: demodulators.length).
"""
from __future__ import annotations

import asyncio
import logging
import math
import sys
from typing import Optional

from sdrsync.websdr.browser_shim import BrowserError as PlaywrightError
from sdrsync.websdr.browser_shim import PageLike as Page
from sdrsync.websdr.browser_shim import click_element_if_present, element_is_present

from sdrsync.websdr.base import WebSDRIncompatibleError, WebSDRStatus, same_site

logger = logging.getLogger("sdrsync.websdr.openwebrx")

LOAD_TIMEOUT_MS = 15000
FREQ_VERIFY_TOLERANCE_HZ = 10
# Per-attempt timeout for _watch_for_audio_unlock_overlay()'s retry loop,
# NOT a total give-up deadline -- see kiwisdr.py's identical constant for
# the full reasoning (confirmed live there that even a generous one-shot
# 20s window wasn't always enough; treated the same way here since this
# is the same class of watcher).
AUDIO_UNLOCK_WATCH_ATTEMPT_S = 5.0
# Settle time after a click before re-probing to confirm the overlay
# actually went away -- see kiwisdr.py's identical constant. OpenWebRX's
# own hideOverlay() fades it out via a CSS transition rather than
# removing it instantly.
AUDIO_UNLOCK_OVERLAY_FADE_S = 1.2

# hamlib mode name -> OpenWebRX modulation string. Confirmed live via
# Modes.getModes() that these carry no narrow/wide suffix convention (unlike
# websdr_org's ...N suffix or KiwiSDR's usn/lsn/cwn/etc.), so this is a
# plain 1:1 dict -- not an oversight that passband_hz isn't used here.
_MODE_MAP: dict[str, str] = {
    "USB": "usb",
    "PKTUSB": "usb",
    "DATA-U": "usb",
    "LSB": "lsb",
    "PKTLSB": "lsb",
    "DATA-L": "lsb",
    "CW": "cw",
    "CWR": "cw",
    "CW-U": "cw",
    "CW-L": "cw",
    "AM": "am",
    "FM": "nfm",
    "WFM": "wfm",
}


def map_hamlib_mode_openwebrx(hamlib_mode: str) -> Optional[str]:
    """Pure mapping from a hamlib mode name to an OpenWebRX modulation
    string. Returns None if there's no known mapping (caller should skip
    mode sync and log, not raise). No passband parameter, unlike
    map_hamlib_mode/map_hamlib_mode_kiwi -- see module docstring."""
    return _MODE_MAP.get(hamlib_mode.upper())


def _fmt_khz(hz) -> str:
    """Human-readable kHz formatting for error messages -- same convention
    as kiwisdr.py and the GUI (f"{hz/1000:.3f} kHz") instead of a raw Hz
    integer, which is hard to read at a glance for HF/VHF frequencies."""
    if hz is None:
        return "unknown"
    return f"{hz / 1000:.3f} kHz"


# JS readiness predicate for attach()'s wait_for_function -- checks the
# EXACT access path every control call below uses
# ($('#openwebrx-panel-receiver').demodulatorPanel().getDemodulator()),
# not just that a sibling global function exists, so a page that passes
# this check is actually ready for tune_hz()/set_mode() to succeed.
# Wrapped in try/catch so a predicate that throws mid-page-init fails the
# wait cleanly (Playwright's wait_for_function rejects immediately on a
# thrown predicate rather than continuing to poll).
_READY_PREDICATE = """
() => {
    try {
        if (typeof $ !== 'function' || !window.ws || window.ws.readyState !== 1) return false;
        var panel = $('#openwebrx-panel-receiver').demodulatorPanel();
        if (!panel) return false;
        var demod = panel.getDemodulator();
        return !!demod && demod.started === true
            && Number.isFinite(window.center_freq)
            && Number.isFinite(window.bandwidth) && window.bandwidth > 0;
    } catch (e) {
        return false;
    }
}
"""


# get_status()'s already-normalized mode string (mode_str.upper(), the
# exact post-normalization set -- USB/LSB/CW/AM/NFM/WFM) -> canonical
# hamlib mode name. Unlike the other two drivers there's no narrow/wide
# suffix convention on this site at all (see the forward _MODE_MAP's own
# comment), so this reverse map is a plain 1:1 dict too.
_REVERSE_MODE_MAP: dict[str, str] = {
    "USB": "USB",
    "LSB": "LSB",
    "CW": "CW",
    "AM": "AM",
    "NFM": "FM",
    "WFM": "WFM",
}


def map_openwebrx_mode_to_hamlib(openwebrx_mode: Optional[str]) -> Optional[str]:
    """Pure reverse mapping: OpenWebRX's get_status()-normalized mode
    string (already uppercased) -> a canonical hamlib mode name. None if
    unknown (caller should skip the reverse push and log, not raise)."""
    if openwebrx_mode is None:
        return None
    return _REVERSE_MODE_MAP.get(openwebrx_mode.upper())


class OpenWebRXDriver:
    """WebSDRDriver implementation for the OpenWebRX software family.

    Does not own the Playwright Page/Browser lifecycle -- see
    WebsdrOrgDriver's docstring, same contract.
    """

    FINGERPRINT_MARKERS = ("compiled/receiver.js",)
    # CW and CWR both forward-map to page mode "cw" (see the module-level
    # mode map above) and _REVERSE_MODE_MAP only has "CW" -> "CW" --
    # ambiguous.
    CW_VARIANT_IS_AMBIGUOUS = True

    def __init__(self, url: str, cw_offset_hz: int = 0, auto_click_audio_unlock: bool = True) -> None:
        self.url = url
        self.cw_offset_hz = cw_offset_hz
        self._auto_click_audio_unlock = auto_click_audio_unlock

        self._page: Optional[Page] = None
        self._current_mode: Optional[str] = None
        self._attached = False
        self._listeners_registered = False
        self._last_attach_error: Optional[str] = None
        self._last_page_error: Optional[str] = None
        self._last_tune_error: Optional[str] = None
        self._last_mode_error: Optional[str] = None

        # Cached from the most recent get_status() (which the engine calls
        # every tick, always before tune_hz/set_mode within the same tick --
        # see sync/engine.py::_tick()), used to (a) skip a page round-trip
        # entirely for a frequency already known to be out of the current
        # profile's range instead of retrying it every 200ms forever, and
        # (b) detect the profile's center/bandwidth changing underneath the
        # driver (e.g. someone switches profile in the browser tab).
        self._center_freq: Optional[int] = None
        self._bandwidth: Optional[int] = None
        self._last_out_of_range_key: Optional[tuple[int, int, int]] = None
        # Tracks the last hamlib mode that had no OpenWebRX equivalent, so
        # the "no equivalent" warning logs once per new occurrence instead
        # of every poll tick the engine retries set_mode() -- same
        # rate-limiting pattern as _last_out_of_range_key above.
        self._last_unmapped_mode: Optional[str] = None
        # Background audio-unlock watcher (see attach()) -- tracked so
        # close() can cancel a still-pending one rather than leaving it
        # running against a page this driver no longer owns.
        self._audio_unlock_task: Optional[asyncio.Task] = None

    @property
    def attached(self) -> bool:
        return self._attached

    # ------------------------------------------------------------------
    async def attach(self, page: Page) -> None:
        self._page = page
        if not self._listeners_registered:
            page.on("console", self._on_console)
            page.on("pageerror", self._on_pageerror)
            self._listeners_registered = True

        try:
            # A reattach can be triggered by get_status() noticing the SDR
            # profile's center/bandwidth changed (see below) -- that means
            # THIS page is already loaded and may already be ready, e.g.
            # because the operator manually switched profile in the
            # browser tab. Re-navigating unconditionally would silently
            # discard that choice back to the server's default profile, so
            # check readiness in place first and only goto() if it's not
            # already satisfied (a fresh, never-navigated page safely
            # evaluates to "not ready" here rather than erroring).
            # _READY_PREDICATE alone isn't enough to decide that, though --
            # it's true for ANY ready OpenWebRX instance, including a
            # DIFFERENT site than self.url that happens to still be loaded
            # (e.g. engine.py's _switch_websdr() reusing the same page for
            # a switch between two OpenWebRX stations). Gate on same_site()
            # first so a genuine site switch always navigates.
            current_url = await page.evaluate("() => window.location.href")
            already_ready = same_site(current_url, self.url) and await page.evaluate(_READY_PREDICATE)
            if not already_ready:
                await page.goto(self.url, timeout=LOAD_TIMEOUT_MS)
                await page.wait_for_function(_READY_PREDICATE, timeout=LOAD_TIMEOUT_MS)
                # Best-effort audio unlock, fresh-navigation only (not on
                # a reattach reusing an already-ready page -- the overlay
                # only ever appears once per real page load, so watching
                # for it there too would just be wasted work on every
                # profile-switch reattach). Confirmed live in OpenWebRX's
                # own JS: AudioEngine only starts once its AudioContext
                # reaches "running", which on WebKitGTK (a native, non-
                # WSL2 Linux desktop -- confirmed live) doesn't happen on
                # its own; the page shows #openwebrx-autoplay-overlay
                # (click -> audioEngine.resume()) whenever
                # audioEngine.isAllowed() (== audioContext.state ===
                # 'running') is false at page-init, and nothing else on
                # the page ever calls resume() itself.
                #
                # Run as a background task, NOT awaited here -- same
                # reasoning as kiwisdr.py's identical watcher: confirmed
                # live (for KiwiSDR; treated the same way here) that the
                # overlay's own trigger can fire well after this point, so
                # awaiting it inline would either miss a late-appearing
                # overlay or make attach() hang around for it. A previous
                # still-pending watcher is cancelled first.
                #
                # Skipped entirely on Windows, not just left to no-op:
                # confirmed from OpenWebRX's own source (isAllowed() above)
                # that this overlay is gated on the exact same
                # AudioContext.state check ensure_webview_backend()
                # (browser/backend.py) already guarantees "running" for
                # via --autoplay-policy=no-user-gesture-required -- so the
                # overlay provably never renders there, and watching for
                # one that can never appear would only ever be wasted work
                # on Windows (v2.2.6 behavior confirmed working there;
                # this must not regress it).
                if self._audio_unlock_task is not None:
                    self._audio_unlock_task.cancel()
                if sys.platform != "win32" and self._auto_click_audio_unlock:
                    self._audio_unlock_task = asyncio.create_task(self._watch_for_audio_unlock_overlay(page))
        except PlaywrightError as e:
            self._last_attach_error = (
                f"Page at {self.url} did not behave like a compatible OpenWebRX "
                f"within {LOAD_TIMEOUT_MS}ms (demodulator/control WebSocket not "
                f"found ready): {e!r}"
            )
            raise WebSDRIncompatibleError(self._last_attach_error) from e

        self._attached = True
        self._current_mode = None
        self._center_freq = None
        self._bandwidth = None
        self._last_out_of_range_key = None
        self._last_attach_error = None
        self._last_page_error = None
        self._last_tune_error = None
        self._last_mode_error = None
        self._last_unmapped_mode = None

    async def _watch_for_audio_unlock_overlay(self, page: Page) -> None:
        """Background task launched from attach() -- see its own comment
        for why this isn't just awaited inline. Loops in
        AUDIO_UNLOCK_WATCH_ATTEMPT_S-sized attempts for as long as this
        driver stays attached (bounded only by close()/a reattach
        cancelling this task) -- see kiwisdr.py's identical watcher for why
        a single fixed window isn't reliable enough. Never raises out to
        the event loop's default task-exception logging: a page-closed/
        navigated-away BrowserError here is routine (the operator
        disconnected or switched sites while this was still watching), not
        a real error."""
        try:
            while self._attached:
                # Re-checked every loop iteration -- see kiwisdr.py's
                # identical watcher for why (a bug-hunter review pass
                # found unchecking the Behaviour-tab mouse-hijack setting
                # mid-session had no effect on an already-running
                # watcher).
                if not self._auto_click_audio_unlock:
                    await asyncio.sleep(1.0)
                    continue
                clicked = await click_element_if_present(
                    page, "#openwebrx-autoplay-overlay", timeout_s=AUDIO_UNLOCK_WATCH_ATTEMPT_S,
                )
                if not clicked:
                    continue
                # A dispatched click is not proof it did anything -- see
                # kiwisdr.py's identical watcher (confirmed live there
                # for a receiver requiring an operator-typed ID first;
                # applied here too since it's the same click-doesn't-
                # guarantee-effect risk, even though not specifically
                # confirmed on an OpenWebRX instance). Only trust success
                # once a re-probe confirms the overlay is actually gone
                # -- OpenWebRX's own hideOverlay() fades it out via a CSS
                # transition rather than removing it instantly.
                await asyncio.sleep(AUDIO_UNLOCK_OVERLAY_FADE_S)
                if await element_is_present(page, "#openwebrx-autoplay-overlay"):
                    logger.debug(
                        "Audio-unlock overlay at %s still present after being clicked; "
                        "will keep watching", self.url,
                    )
                    continue
                logger.info("Audio-unlock overlay clicked at %s", self.url)
                return
            logger.debug("Audio-unlock overlay watcher stopped (no longer attached) at %s", self.url)
        except asyncio.CancelledError:
            raise
        except PlaywrightError as e:
            logger.debug("Audio-unlock overlay watcher stopped early (non-fatal): %s", e)

    # ------------------------------------------------------------------
    async def tune_hz(self, freq_hz: int, verify: bool = True) -> bool:
        """Returns True only if actually applied and verified via readback.

        verify is accepted for WebSDRDriver Protocol parity but unused
        here -- this driver's readback verification is synchronous and
        inline (below), not a delayed background task, so there's nothing
        for verify=False to skip."""
        if not self._attached:
            return False
        effective_hz = freq_hz
        if self._current_mode == "cw":
            effective_hz += self.cw_offset_hz

        # Cheap local pre-check using the values the last get_status() saw
        # -- avoids a page round-trip (and a fresh warning log line) for a
        # frequency that's already known to be outside the active
        # profile's range and hasn't changed, which would otherwise happen
        # every poll tick for as long as the rig sits there.
        if self._center_freq is not None and self._bandwidth is not None:
            if abs(effective_hz - self._center_freq) > self._bandwidth / 2:
                key = (effective_hz, self._center_freq, self._bandwidth)
                if key != self._last_out_of_range_key:
                    self._last_out_of_range_key = key
                    self._last_tune_error = self._out_of_range_message(effective_hz)
                    logger.warning(self._last_tune_error)
                else:
                    logger.debug("Frequency still out of range (unchanged): %s", effective_hz)
                return False

        try:
            result = await self._page.evaluate(
                "(freq_hz) => { "
                "if (!window.ws || window.ws.readyState !== 1) return {status: 'not_ready'}; "
                "var cf = window.center_freq, bw = window.bandwidth; "
                "if (!Number.isFinite(cf) || !Number.isFinite(bw) || bw <= 0) return {status: 'not_ready'}; "
                "var offset = Math.round(freq_hz - cf); "
                "if (Math.abs(offset) > bw / 2) return {status: 'out_of_range', center_freq: cf, bandwidth: bw}; "
                "var demod = $('#openwebrx-panel-receiver').demodulatorPanel().getDemodulator(); "
                "demod.set_offset_frequency(offset); "
                "return {status: 'ok', offset: demod.offset_frequency, center_freq: cf, bandwidth: bw}; "
                "}",
                effective_hz,
            )
        except PlaywrightError as e:
            self._last_tune_error = f"set_offset_frequency() failed: {e}"
            logger.warning(self._last_tune_error)
            return False

        status = result.get("status")
        if status == "not_ready":
            self._last_tune_error = "OpenWebRX control WebSocket is not open (disconnected mid-session?)"
            logger.warning(self._last_tune_error)
            self._attached = False
            return False

        self._update_profile_cache(result.get("center_freq"), result.get("bandwidth"))

        if status == "out_of_range":
            key = (effective_hz, result.get("center_freq"), result.get("bandwidth"))
            self._last_tune_error = self._out_of_range_message(effective_hz)
            if key != self._last_out_of_range_key:
                logger.warning(self._last_tune_error)
            else:
                logger.debug("Frequency still out of range (unchanged): %s", effective_hz)
            self._last_out_of_range_key = key
            return False

        actual_offset = result.get("offset")
        if not isinstance(actual_offset, (int, float)) or not math.isfinite(actual_offset):
            self._last_tune_error = "Could not read back offset_frequency to verify set_offset_frequency()"
            logger.warning(self._last_tune_error)
            return False
        actual_hz = self._center_freq + actual_offset if self._center_freq is not None else None
        if actual_hz is None or abs(actual_hz - effective_hz) > FREQ_VERIFY_TOLERANCE_HZ:
            self._last_tune_error = (
                f"OpenWebRX did not apply requested frequency: wanted {_fmt_khz(effective_hz)}, "
                f"reads {_fmt_khz(actual_hz)}"
            )
            logger.warning(self._last_tune_error)
            return False

        self._last_tune_error = None
        self._last_out_of_range_key = None
        return True

    def _out_of_range_message(self, effective_hz: int) -> str:
        if self._center_freq is None or self._bandwidth is None:
            return f"Requested frequency {_fmt_khz(effective_hz)} is outside the active SDR profile's range"
        lo = self._center_freq - self._bandwidth // 2
        hi = self._center_freq + self._bandwidth // 2
        return (
            f"Requested frequency {_fmt_khz(effective_hz)} is outside the active SDR profile's "
            f"range ({_fmt_khz(lo)} - {_fmt_khz(hi)}); switching profiles automatically isn't supported yet"
        )

    def _update_profile_cache(self, center_freq, bandwidth) -> None:
        if isinstance(center_freq, (int, float)) and math.isfinite(center_freq):
            self._center_freq = int(center_freq)
        if isinstance(bandwidth, (int, float)) and math.isfinite(bandwidth) and bandwidth > 0:
            self._bandwidth = int(bandwidth)

    # ------------------------------------------------------------------
    async def set_mode(self, hamlib_mode: str, passband_hz: Optional[int]) -> bool:
        """Returns True only if actually applied and verified via readback.
        passband_hz is accepted for WebSDRDriver interface compatibility
        but unused -- OpenWebRX's modulation strings have no narrow/wide
        variant (see map_hamlib_mode_openwebrx)."""
        if not self._attached:
            return False
        mode = map_hamlib_mode_openwebrx(hamlib_mode)
        if mode is None:
            self._last_mode_error = (
                f"hamlib mode {hamlib_mode!r} has no OpenWebRX equivalent; frequency sync continues"
            )
            if hamlib_mode != self._last_unmapped_mode:
                self._last_unmapped_mode = hamlib_mode
                logger.warning(self._last_mode_error)
            else:
                logger.debug(self._last_mode_error)
            return False

        try:
            result = await self._page.evaluate(
                "(mode) => { "
                "if (!window.ws || window.ws.readyState !== 1) return 'not_ready'; "
                "$('#openwebrx-panel-receiver').demodulatorPanel().setMode(mode); "
                "return 'ok'; "
                "}",
                mode,
            )
        except PlaywrightError as e:
            self._last_mode_error = f"setMode() failed: {e}"
            logger.warning(self._last_mode_error)
            return False

        if result == "not_ready":
            self._last_mode_error = "OpenWebRX control WebSocket is not open (disconnected mid-session?)"
            logger.warning(self._last_mode_error)
            self._attached = False
            return False

        actual_mode = await self._read_mode()
        if actual_mode != mode:
            self._last_mode_error = (
                f"OpenWebRX did not apply requested mode: wanted {mode!r}, reads {actual_mode!r}"
            )
            logger.warning(self._last_mode_error)
            return False

        self._current_mode = mode
        self._last_mode_error = None
        self._last_unmapped_mode = None
        return True

    async def _read_mode(self) -> Optional[str]:
        try:
            return await self._page.evaluate(
                "() => { var d = $('#openwebrx-panel-receiver').demodulatorPanel().getDemodulator(); "
                "return d ? d.modulation : null; }"
            )
        except PlaywrightError:
            return None

    # Mute-on-TX is handled natively at the page-adapter level now (v15,
    # WxPageAdapter.set_muted() in browser_shim.py) -- see its module
    # comment for why: a native WebView-level mute is instant and site-
    # independent, unlike this OpenWebRX-specific toggleMute() JS call.

    def _reverse_effective_hz(
        self, observed_hz: Optional[int], observed_hamlib_mode: Optional[str]
    ) -> Optional[int]:
        """Un-applies cw_offset_hz for the reverse direction (WebSDR ->
        rig), symmetric to tune_hz()'s forward application. Takes the
        mode as an explicit argument, sourced from the SAME status
        snapshot map_openwebrx_mode_to_hamlib() derived it from -- NOT
        self._current_mode, which is stale by construction for reverse
        sync (see websdr_org.py's identical helper for the full
        reasoning)."""
        if observed_hz is None:
            return None
        if observed_hamlib_mode == "CW":
            return observed_hz - self.cw_offset_hz
        return observed_hz

    def hamlib_mode_from_status(self, status: WebSDRStatus) -> Optional[str]:
        return map_openwebrx_mode_to_hamlib(status.mode)

    def rig_freq_from_status(self, status: WebSDRStatus) -> Optional[int]:
        return self._reverse_effective_hz(status.freq_hz, self.hamlib_mode_from_status(status))

    # ------------------------------------------------------------------
    def _combined_error(self) -> Optional[str]:
        parts = [
            e for e in (
                self._last_attach_error, self._last_page_error,
                self._last_tune_error, self._last_mode_error,
            ) if e
        ]
        return " | ".join(parts) if parts else None

    async def get_status(self) -> WebSDRStatus:
        if not self._attached:
            return WebSDRStatus(connected=False, last_error=self._combined_error())
        try:
            data = await self._page.evaluate(
                "() => { "
                "var demod = $('#openwebrx-panel-receiver').demodulatorPanel().getDemodulator(); "
                "return {center_freq: window.center_freq, bandwidth: window.bandwidth, "
                "offset: demod ? demod.offset_frequency : null, "
                "mode: demod ? demod.modulation : null, "
                "ws_ready: window.ws ? window.ws.readyState === 1 : false}; "
                "}"
            )
            center_freq = data.get("center_freq")
            bandwidth = data.get("bandwidth")
            offset = data.get("offset")
            mode_str = data.get("mode")

            # A dropped control WebSocket is otherwise invisible: the
            # page's globals stay readable (this evaluate keeps
            # succeeding), and once the rig settles on a frequency the
            # engine stops calling tune_hz/set_mode altogether (their own
            # readyState re-checks -- the actual recovery trigger -- would
            # then never run), leaving the driver silently wedged
            # "connected" until the rig's value next changes.
            if not data.get("ws_ready"):
                self._attached = False
                self._last_page_error = "OpenWebRX control WebSocket is not open (disconnected mid-session?)"
                logger.warning(self._last_page_error)
                return WebSDRStatus(connected=False, last_error=self._combined_error())

            # Each field is checked for finiteness independently before any
            # arithmetic -- a transient non-finite reading here must not
            # raise (which would otherwise flip self._attached off on a
            # perfectly healthy page, the same bug class already hit once
            # for KiwiSDR's frequency readback).
            center_freq_ok = isinstance(center_freq, (int, float)) and math.isfinite(center_freq)
            bandwidth_ok = isinstance(bandwidth, (int, float)) and math.isfinite(bandwidth) and bandwidth > 0
            offset_ok = isinstance(offset, (int, float)) and math.isfinite(offset)

            # The active SDR profile's center/bandwidth changing underneath
            # the driver (someone switches profile in the browser tab, or
            # the server pushes a new one) would otherwise leave the
            # demodulator's stale offset silently pointing at the wrong
            # absolute frequency forever, since the rig's own frequency
            # hasn't moved so nothing would re-trigger a push. Detaching
            # here routes recovery through the existing attach-supervisor
            # path, which resets the sync latches and forces a fresh push
            # at the new center once re-attached.
            if (
                center_freq_ok and bandwidth_ok
                and self._center_freq is not None and self._bandwidth is not None
                and (int(center_freq) != self._center_freq or int(bandwidth) != self._bandwidth)
            ):
                self._attached = False
                self._last_page_error = (
                    f"OpenWebRX SDR profile changed underneath the driver "
                    f"(center/bandwidth {self._center_freq}/{self._bandwidth} -> "
                    f"{int(center_freq)}/{int(bandwidth)}); reattaching"
                )
                logger.warning(self._last_page_error)
                return WebSDRStatus(connected=False, last_error=self._combined_error())

            if center_freq_ok and bandwidth_ok:
                self._update_profile_cache(center_freq, bandwidth)

            freq_hz = int(center_freq) + int(offset) if center_freq_ok and offset_ok else None

            return WebSDRStatus(
                connected=True,
                freq_hz=freq_hz,
                mode=mode_str.upper() if mode_str else None,
                # Proxy: "is the control socket open", not a true audio-is-
                # actually-playing signal, same documented caveat as
                # KiwiSDR's proxy.
                audio_active=data.get("ws_ready"),
                last_error=self._combined_error(),
            )
        except PlaywrightError as e:
            self._attached = False
            self._last_page_error = str(e)
            return WebSDRStatus(connected=False, last_error=self._combined_error())

    async def close(self) -> None:
        if self._audio_unlock_task is not None:
            self._audio_unlock_task.cancel()
            self._audio_unlock_task = None
        self._attached = False
        self._page = None

    # ------------------------------------------------------------------
    def _on_console(self, msg) -> None:
        if msg.type in ("error", "warning"):
            logger.warning("[OpenWebRX page console:%s] %s", msg.type, msg.text)

    def _on_pageerror(self, exc) -> None:
        self._last_page_error = f"Unhandled JS error on OpenWebRX page: {exc}"
        logger.error(self._last_page_error)
