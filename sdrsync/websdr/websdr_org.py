"""Driver for the classic "WebSDR" software from websdr.org (by PA3FWM;
e.g. websdr.ewi.utwente.nl).

Strategy: rather than reimplementing the site's custom binary
websocket/audio protocol, drive the real page's own control functions
(confirmed by reading the live site's actual JS source) via
Playwright's page.evaluate. This gets genuine audio playback for free
through the real browser tab, and is resilient in the sense that any
control-API break is fail-loud (WebSDRIncompatibleError) rather than a
silent protocol mismatch.

Confirmed global functions on the page:
    setfreq(khz: float)     -- set receive frequency within the current band
    set_mode(mode: str)     -- "USB"/"USBN"/"LSB"/"LSBN"/"AM"/"AMN"/"FM"/"CW"/"AMSYNC"
    setband(idx: int)       -- switch which (possibly non-overlapping) band is active
    soundapplet.mute(1|0)   -- true mute/unmute of the WebSocket audio stream
    #freqinput               -- text input reflecting the current frequency (kHz)

The Twente instance itself has a single band (nbands=1) spanning ~0-29 MHz,
so band switching is a no-op there in practice, but the logic is generic so
other/future websdr.org instances with multiple bands work unmodified.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from sdrsync.websdr.browser_shim import BrowserError as PlaywrightError
from sdrsync.websdr.browser_shim import PageLike as Page

# TargetClosedError (browser/tab closed) is a subclass of playwright's base
# Error in the versions we support, so catching PlaywrightError alone covers
# it -- avoids depending on an export that isn't present in every version.

from sdrsync.websdr.base import WebSDRDriver, WebSDRIncompatibleError, WebSDRStatus

logger = logging.getLogger("sdrsync.websdr.websdr_org")

LOAD_TIMEOUT_MS = 15000
FREQ_VERIFY_DELAY_S = 0.6
FREQ_VERIFY_TOLERANCE_HZ = 10
AUDIO_GATE_CHECK_DELAY_S = 1.0
NARROW_USB_LSB_THRESHOLD_HZ = 2000
NARROW_AM_THRESHOLD_HZ = 6000

_NUMERIC_RE = re.compile(r"[-+]?\d*\.\d+|\d+")

# hamlib mode name -> (webSDR base mode, supports an "N" narrow variant)
_MODE_MAP: dict[str, tuple[str, bool]] = {
    "USB": ("USB", True),
    "PKTUSB": ("USB", True),
    "LSB": ("LSB", True),
    "PKTLSB": ("LSB", True),
    "CW": ("CW", False),
    "CWR": ("CW", False),
    "AM": ("AM", True),
    "SAM": ("AMSYNC", False),
    "FM": ("FM", False),
    "WFM": ("FM", False),
}


def map_hamlib_mode(hamlib_mode: str, passband_hz: Optional[int]) -> Optional[str]:
    """Pure mapping from a hamlib mode name (+ optional passband) to a WebSDR
    mode string. Returns None if there's no known mapping (caller should skip
    mode sync and log, not raise)."""
    mapped = _MODE_MAP.get(hamlib_mode.upper())
    if mapped is None:
        return None
    base_mode, supports_narrow = mapped
    if supports_narrow and passband_hz:
        threshold = NARROW_AM_THRESHOLD_HZ if base_mode == "AM" else NARROW_USB_LSB_THRESHOLD_HZ
        if passband_hz < threshold:
            return base_mode + "N"
    return base_mode


class WebsdrOrgDriver:
    """WebSDRDriver implementation for the websdr.org (PA3FWM) WebSDR software family.

    The driver does not own the Playwright Page/Browser lifecycle -- the
    caller (sync engine) launches the browser and page and is responsible
    for closing them; this driver only navigates and controls the page it's
    given via attach().
    """

    FINGERPRINT_MARKERS = ("websdr-base.js",)

    def __init__(self, url: str, cw_offset_hz: int = 0) -> None:
        self.url = url
        self.cw_offset_hz = cw_offset_hz

        self._page: Optional[Page] = None
        self._bands: list[tuple[float, float]] = []  # (low_hz, high_hz) per band index
        self._current_band: Optional[int] = None
        self._current_mode: Optional[str] = None
        # True only once attach() has fully succeeded (band table loaded,
        # audio gate handled). tune_hz/set_mode/set_muted/get_status all
        # gate on this -- NOT on `self._page is None` -- because attach()
        # sets self._page as its very first statement, before navigation
        # even starts, so a `_page is None` check is disengaged for the
        # entire retry window, not just after success.
        self._attached = False
        self._listeners_registered = False
        # Kept separate (rather than one shared _last_error) so a successful
        # set_mode() doesn't wipe out a live tune_hz() error and vice versa.
        self._last_tune_error: Optional[str] = None
        self._last_mode_error: Optional[str] = None
        self._last_page_error: Optional[str] = None
        self._last_attach_error: Optional[str] = None
        self._verify_task: Optional[asyncio.Task] = None

    @property
    def attached(self) -> bool:
        return self._attached

    # ------------------------------------------------------------------
    async def attach(self, page: Page) -> None:
        """May be called repeatedly (e.g. by an attach-retry loop after a
        prior failure) with the same page. self._attached only flips to
        True once every step below has succeeded."""
        self._page = page
        if not self._listeners_registered:
            page.on("console", self._on_console)
            page.on("pageerror", self._on_pageerror)
            self._listeners_registered = True

        # page.goto() reloads the site's JS, resetting its band/mode back to
        # default -- so our idea of "current band/mode" from before this
        # attach attempt is stale and must not survive a (re)navigation.
        # Otherwise tune_hz() would skip a needed setband() call after
        # recovering from an outage, silently tuning the wrong band.
        self._current_band = None
        self._current_mode = None

        try:
            await page.goto(self.url, timeout=LOAD_TIMEOUT_MS)
            await page.wait_for_function(
                "typeof window.setfreq === 'function' && typeof window.set_mode === 'function' "
                "&& typeof window.setband === 'function'",
                timeout=LOAD_TIMEOUT_MS,
            )
            await self._load_band_table()
            await self._satisfy_audio_gate()
        except WebSDRIncompatibleError as e:
            self._last_attach_error = str(e)
            raise
        except (PlaywrightError, TypeError, KeyError, ValueError) as e:
            # TypeError/KeyError/ValueError: page-supplied JSON (bandinfo)
            # wasn't shaped the way we expect. Treated the same as a
            # Playwright-level failure -- both mean "this page isn't a
            # compatible websdr.org WebSDR right now" -- so this always
            # surfaces through WebSDRIncompatibleError and never escapes
            # as a raw exception that would silently kill whatever is
            # calling attach() in a retry loop.
            self._last_attach_error = (
                f"Page at {self.url} did not behave like a compatible websdr.org WebSDR "
                f"within {LOAD_TIMEOUT_MS}ms: {e!r}"
            )
            raise WebSDRIncompatibleError(self._last_attach_error) from e

        self._attached = True
        # A fresh, successful attach means any error recorded before or
        # during the outage is no longer current -- clear all of them, not
        # just the attach-specific one, or a stale JS/tune error would keep
        # showing in the GUI forever after a full recovery.
        self._last_attach_error = None
        self._last_page_error = None
        self._last_tune_error = None
        self._last_mode_error = None

    async def _load_band_table(self) -> None:
        try:
            raw = await self._page.evaluate(
                "() => bandinfo.map(b => ({c: b.centerfreq, s: b.samplerate}))"
            )
        except PlaywrightError as e:
            raise WebSDRIncompatibleError(f"Could not read bandinfo from page: {e}") from e
        self._bands = []
        for entry in raw:
            center_hz = float(entry["c"]) * 1000.0
            span_hz = float(entry["s"]) * 1000.0
            self._bands.append((center_hz - span_hz / 2, center_hz + span_hz / 2))
        if not self._bands:
            raise WebSDRIncompatibleError("Page's bandinfo array was empty")
        logger.info("Loaded %d band(s) from %s", len(self._bands), self.url)

    async def _satisfy_audio_gate(self) -> None:
        """Best-effort: satisfy the browser's autoplay-gesture requirement.

        Clicks a corner of the viewport rather than the body's (default)
        center point, since on this site the body center is typically the
        tuning waterfall/spectrum canvas -- clicking there is click-to-tune
        and would retune the receiver. A corner click is very unlikely to
        land on any control.
        """
        try:
            await self._page.mouse.click(2, 2)
        except PlaywrightError:
            pass
        await asyncio.sleep(AUDIO_GATE_CHECK_DELAY_S)
        try:
            state = await self._page.evaluate("() => (document.ct ? document.ct.state : null)")
            if state == "suspended":
                await self._page.evaluate("() => document.ct && document.ct.resume()")
                logger.warning("Audio context was suspended; requested resume()")
        except PlaywrightError as e:
            logger.debug("Could not inspect/resume audio context: %s", e)

    # ------------------------------------------------------------------
    def _band_for_freq(self, freq_hz: int) -> Optional[int]:
        for idx, (low, high) in enumerate(self._bands):
            if low <= freq_hz <= high:
                return idx
        return None

    async def tune_hz(self, freq_hz: int) -> bool:
        """Returns True only if the frequency was actually pushed to the
        page. The caller (SyncEngine) must only update its "last sent"
        bookkeeping when this is True -- otherwise a no-op (not attached, or
        a failed page.evaluate) would get recorded as delivered, and the
        engine would never retry it once the WebSDR recovers."""
        if not self._attached:
            return False
        effective_hz = freq_hz
        if self._current_mode in ("CW",):
            effective_hz += self.cw_offset_hz

        band_idx = self._band_for_freq(effective_hz)
        if band_idx is None:
            self._last_tune_error = f"{effective_hz/1000:.3f} kHz is outside all bands this WebSDR covers"
            logger.warning(self._last_tune_error)
            return False

        try:
            if band_idx != self._current_band:
                await self._page.evaluate("(idx) => window.setband(idx)", band_idx)
                self._current_band = band_idx
            await self._page.evaluate("(khz) => window.setfreq(khz)", effective_hz / 1000.0)
            self._last_tune_error = None
        except PlaywrightError as e:
            self._last_tune_error = f"setfreq() failed: {e}"
            logger.warning(self._last_tune_error)
            return False

        self._schedule_freq_verification(effective_hz)
        return True

    def _schedule_freq_verification(self, expected_hz: int) -> None:
        if self._verify_task is not None and not self._verify_task.done():
            self._verify_task.cancel()
        self._verify_task = asyncio.ensure_future(self._verify_freq_applied(expected_hz))

    async def _read_freqinput_hz(self) -> Optional[int]:
        value = await self._page.evaluate(
            "() => document.getElementById('freqinput') ? document.getElementById('freqinput').value : null"
        )
        match = _NUMERIC_RE.search(value) if value else None
        if not match:
            return None
        return int(round(float(match.group()) * 1000))

    async def _verify_freq_applied(self, expected_hz: int) -> None:
        try:
            await asyncio.sleep(FREQ_VERIFY_DELAY_S)
            if not self._attached:
                return
            actual_hz = await self._read_freqinput_hz()
            if actual_hz is None:
                logger.warning("Could not read back #freqinput to verify setfreq()")
                return
            if abs(actual_hz - expected_hz) <= FREQ_VERIFY_TOLERANCE_HZ:
                return

            logger.warning(
                "WebSDR did not apply requested frequency: wanted %.3f kHz, page shows %.3f kHz. "
                "Retrying band selection once.",
                expected_hz / 1000, actual_hz / 1000,
            )
            # One retry: force band re-selection in case setfreq was silently
            # ignored because we were at a band edge.
            band_idx = self._band_for_freq(expected_hz)
            if band_idx is None:
                self._last_tune_error = f"WebSDR frequency mismatch and {expected_hz/1000:.3f} kHz is outside all known bands"
                return
            await self._page.evaluate("(idx) => window.setband(idx)", band_idx)
            self._current_band = band_idx
            await self._page.evaluate("(khz) => window.setfreq(khz)", expected_hz / 1000.0)

            await asyncio.sleep(FREQ_VERIFY_DELAY_S)
            actual_hz = await self._read_freqinput_hz()
            if actual_hz is not None and abs(actual_hz - expected_hz) <= FREQ_VERIFY_TOLERANCE_HZ:
                self._last_tune_error = None
            else:
                self._last_tune_error = "WebSDR frequency mismatch persists after retry; see log"
        except asyncio.CancelledError:
            pass
        except PlaywrightError as e:
            logger.debug("Frequency verification failed (non-fatal): %s", e)

    # ------------------------------------------------------------------
    async def set_mode(self, hamlib_mode: str, passband_hz: Optional[int]) -> bool:
        """Returns True only if the mode was actually pushed to the page --
        see tune_hz()'s docstring for why the caller must gate its "last
        sent" bookkeeping on this rather than assuming the call worked."""
        if not self._attached:
            return False
        web_mode = map_hamlib_mode(hamlib_mode, passband_hz)
        if web_mode is None:
            self._last_mode_error = (
                f"hamlib mode {hamlib_mode!r} has no WebSDR equivalent; frequency sync continues"
            )
            logger.warning(self._last_mode_error)
            return False

        try:
            await self._page.evaluate("(m) => window.set_mode(m)", web_mode)
            # base mode without the "N" (narrow) suffix, used e.g. to detect CW for cw_offset_hz
            self._current_mode = web_mode[:-1] if web_mode.endswith("N") else web_mode
            self._last_mode_error = None
            return True
        except PlaywrightError as e:
            self._last_mode_error = f"set_mode() failed: {e}"
            logger.warning(self._last_mode_error)
            return False

    async def set_muted(self, muted: bool) -> None:
        if not self._attached:
            return
        try:
            await self._page.evaluate(
                "(m) => { if (window.soundapplet) window.soundapplet.mute(m ? 1 : 0); }", muted
            )
        except PlaywrightError as e:
            logger.debug("mute() call failed (non-fatal): %s", e)

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
                "() => ({freq: window.freq, mode: window.mode, "
                "audio: document.ct ? document.ct.state === 'running' : null})"
            )
            return WebSDRStatus(
                connected=True,
                freq_hz=int(round(float(data["freq"]) * 1000)) if data.get("freq") is not None else None,
                mode=data.get("mode"),
                audio_active=data.get("audio"),
                last_error=self._combined_error(),
            )
        except PlaywrightError as e:
            # The page died out from under us -- drop back to "not attached"
            # so tune_hz/set_mode/set_muted become no-ops again until a
            # fresh attach() (retried by the engine) succeeds.
            self._attached = False
            self._last_page_error = str(e)
            return WebSDRStatus(connected=False, last_error=self._combined_error())

    async def close(self) -> None:
        if self._verify_task is not None and not self._verify_task.done():
            self._verify_task.cancel()
            try:
                await self._verify_task
            except (asyncio.CancelledError, PlaywrightError):
                pass
        self._attached = False
        self._page = None

    # ------------------------------------------------------------------
    def _on_console(self, msg) -> None:
        if msg.type in ("error", "warning"):
            logger.warning("[WebSDR page console:%s] %s", msg.type, msg.text)

    def _on_pageerror(self, exc) -> None:
        self._last_page_error = f"Unhandled JS error on WebSDR page: {exc}"
        logger.error(self._last_page_error)
