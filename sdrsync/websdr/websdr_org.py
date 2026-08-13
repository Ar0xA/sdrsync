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
    freq (global var)       -- the current carrier/BFO frequency in kHz, as
                               last stored by setfreq(); this is what the page
                               sends upstream ("f=" + freq.toFixed(3)), so it
                               carries full 1 Hz resolution. Note the VISIBLE
                               frequency box (document.freqform.frequency) is
                               only nomfreq.toFixed(2), i.e. rounded to 10 Hz --
                               a sub-10 Hz retune is real but invisible there.
                               There is no #freqinput element on any websdr.org
                               build (verified live against five instances).

The Twente instance itself has a single band (nbands=1) spanning ~0-29 MHz,
so band switching is a no-op there in practice, but the logic is generic so
other/future websdr.org instances with multiple bands work unmodified.
"""
from __future__ import annotations

import asyncio
import logging
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

# hamlib mode name -> (webSDR base mode, supports an "N" narrow variant)
_MODE_MAP: dict[str, tuple[str, bool]] = {
    "USB": ("USB", True),
    "PKTUSB": ("USB", True),
    "DATA-U": ("USB", True),
    "LSB": ("LSB", True),
    "PKTLSB": ("LSB", True),
    "DATA-L": ("LSB", True),
    "CW": ("CW", False),
    "CWR": ("CW", False),
    "CW-U": ("CW", False),
    "CW-L": ("CW", False),
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


# WebSDR base mode string (post-N-stripping, the exact set get_status()
# already normalizes to -- see get_status()'s own mode-stripping logic)
# -> canonical hamlib mode name. Reverse of _MODE_MAP above, but NOT
# mechanically derived from it: _MODE_MAP is many-to-one (e.g. USB/
# PKTUSB/DATA-U all forward-map to "USB"), so each collision here is an
# explicit, deliberate choice of ONE canonical hamlib name to reverse-map
# back to -- never the data/CW variants, which this driver's forward
# direction also never sends to this site in the first place.
_REVERSE_MODE_MAP: dict[str, str] = {
    "USB": "USB",
    "LSB": "LSB",
    "CW": "CW",
    "AM": "AM",
    "AMSYNC": "SAM",
    "FM": "FM",
}


def map_websdr_mode_to_hamlib(websdr_mode: Optional[str]) -> Optional[str]:
    """Pure reverse mapping: a websdr.org base mode string (as already
    normalized by get_status(), i.e. any trailing 'N' narrow suffix
    already stripped) -> a canonical hamlib mode name. None if unknown
    (caller should skip the reverse push and log, not raise)."""
    if websdr_mode is None:
        return None
    return _REVERSE_MODE_MAP.get(websdr_mode.upper())


class WebsdrOrgDriver:
    """WebSDRDriver implementation for the websdr.org (PA3FWM) WebSDR software family.

    The driver does not own the Playwright Page/Browser lifecycle -- the
    caller (sync engine) launches the browser and page and is responsible
    for closing them; this driver only navigates and controls the page it's
    given via attach().
    """

    FINGERPRINT_MARKERS = ("websdr-base.js",)
    # CW/CWR/CW-U/CW-L all forward-map to the single page mode "CW" (see
    # _MODE_MAP above) and _REVERSE_MODE_MAP only has "CW" -> "CW" -- a
    # genuinely ambiguous observation.
    CW_VARIANT_IS_AMBIGUOUS = True

    def __init__(self, url: str, cw_offset_hz: int = 0) -> None:
        self.url = url
        self.cw_offset_hz = cw_offset_hz

        self._page: Optional[Page] = None
        self._bands: list[tuple[float, float]] = []  # (low_hz, high_hz) per band index
        self._current_band: Optional[int] = None
        self._current_mode: Optional[str] = None
        # Full web-mode string as last pushed to window.set_mode(), narrow
        # ("N") suffix included -- unlike self._current_mode (which strips
        # it for the CW-offset check), this is what setband()'s silent
        # mode-flip fixup below needs to restore the EXACT mode (including
        # filter width), not just the base name.
        self._current_web_mode: Optional[str] = None
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
        # Tracks the last hamlib mode that had no WebSDR equivalent, so the
        # "no equivalent" warning logs once per new occurrence instead of
        # every poll tick the engine retries set_mode() (rate-limiting,
        # same pattern as openwebrx.py's _last_out_of_range_key).
        self._last_unmapped_mode: Optional[str] = None
        # Same rate-limiting pattern, for the "outside all bands this
        # WebSDR covers" rejection: the engine retries a rejected forward
        # push on its own schedule, so a rig parked on a frequency this
        # receiver simply doesn't cover would otherwise emit an identical
        # WARNING line indefinitely. Keyed on the effective frequency
        # alone, since the band table it's tested against only changes on
        # attach() -- which resets this too.
        self._last_out_of_range_hz: Optional[int] = None

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
        self._current_web_mode = None

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
        self._last_unmapped_mode = None
        self._last_out_of_range_hz = None

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
            # "suspended" is the only state the Web Audio spec defines besides
            # "running"/"closed", but WebKitGTK (confirmed live on a native
            # Linux desktop, non-WSL2 -- NOT the WSL2/WSLg environment this
            # driver was originally verified against) uses its own extra
            # state, "interrupted", for a context that hasn't been unlocked
            # yet. Only checking "suspended" left resume() never called at
            # all on that platform, so the WebSDR's audio stayed silent
            # forever with no error anywhere -- confirmed root cause via a
            # standalone wx.html2.WebView + AudioContext repro (real click
            # via wx.UIActionSimulator, same mechanism as mouse.click()
            # above, does transition interrupted -> running once resume()
            # is actually called).
            if state in ("suspended", "interrupted"):
                await self._page.evaluate("() => document.ct && document.ct.resume()")
                logger.warning("Audio context was %s; requested resume()", state)
        except PlaywrightError as e:
            logger.debug("Could not inspect/resume audio context: %s", e)

    # ------------------------------------------------------------------
    def _band_for_freq(self, freq_hz: int) -> Optional[int]:
        for idx, (low, high) in enumerate(self._bands):
            if low <= freq_hz <= high:
                return idx
        return None

    async def tune_hz(self, freq_hz: int, verify: bool = True) -> bool:
        """Returns True only if the frequency was actually pushed to the
        page. The caller (SyncEngine) must only update its "last sent"
        bookkeeping when this is True -- otherwise a no-op (not attached, or
        a failed page.evaluate) would get recorded as delivered, and the
        engine would never retry it once the WebSDR recovers.

        verify=False skips arming _schedule_freq_verification() -- see
        WebSDRDriver.tune_hz's docstring for why (periodic full-resync of
        an unchanged frequency must not arm a corrective re-tune that can
        fight a concurrent reverse-sync push)."""
        if not self._attached:
            return False
        effective_hz = freq_hz
        if self._current_mode in ("CW",):
            effective_hz += self.cw_offset_hz

        band_idx = self._band_for_freq(effective_hz)
        if band_idx is None:
            self._last_tune_error = f"{effective_hz/1000:.3f} kHz is outside all bands this WebSDR covers"
            # Rate-limited exactly like _last_unmapped_mode below and
            # openwebrx.py's _last_out_of_range_key: WARNING the first
            # time a given frequency is rejected, DEBUG on exact repeats.
            if effective_hz != self._last_out_of_range_hz:
                logger.warning(self._last_tune_error)
            else:
                logger.debug("Frequency still outside all bands (unchanged): %s", effective_hz)
            self._last_out_of_range_hz = effective_hz
            return False

        # Captured before the write, only when we'll actually verify --
        # lets _verify_freq_applied() tell "our own setfreq() didn't take"
        # apart from "the page moved because the user clicked it" (see
        # that method's docstring). Best-effort: a failed read here must
        # not block the actual frequency push, so it just falls back to
        # None (verify then falls back to its old, conservative behavior).
        pre_push_hz = None
        if verify:
            try:
                pre_push_hz = await self._read_page_freq_hz()
            except PlaywrightError:
                pre_push_hz = None
            # Re-check after the await above: a concurrent close() (e.g.
            # the user hit Disconnect, or SwitchWebsdr tore this driver
            # down) could have run while it was suspended and set
            # self._page to None -- there was no await between the
            # entry check and the first self._page.evaluate() call
            # before pre_push_hz existed, so this window is new here,
            # not pre-existing. Every other await point in this method
            # already re-validates the same way (see
            # _verify_freq_applied's identical check after its sleep).
            if not self._attached:
                return False

        try:
            if band_idx != self._current_band:
                await self._page.evaluate("(idx) => window.setband(idx)", band_idx)
                self._current_band = band_idx
                # setband() silently flips window.mode USB<->LSB (and swaps
                # its hi/lo filter offsets) whenever the crossing changes
                # islsbband() -- confirmed against the live websdr-base.js
                # source, not inferred. Re-assert whatever mode we last
                # pushed so a band crossing can't leave the page on the
                # wrong sideband with no error and no verification catching
                # it (window.freq is unaffected by the flip).
                if self._current_web_mode is not None:
                    await self._page.evaluate("(m) => window.set_mode(m)", self._current_web_mode)
            await self._page.evaluate("(khz) => window.setfreq(khz)", effective_hz / 1000.0)
            self._last_tune_error = None
        except PlaywrightError as e:
            self._last_tune_error = f"setfreq() failed: {e}"
            logger.warning(self._last_tune_error)
            return False

        if verify:
            self._schedule_freq_verification(effective_hz, pre_push_hz)
        return True

    def _schedule_freq_verification(self, expected_hz: int, pre_push_hz: Optional[int] = None) -> None:
        if self._verify_task is not None and not self._verify_task.done():
            self._verify_task.cancel()
        self._verify_task = asyncio.ensure_future(self._verify_freq_applied(expected_hz, pre_push_hz))

    async def _read_page_freq_hz(self) -> Optional[int]:
        """Reads back the frequency the page itself currently holds.

        Reads the page's own global `freq` (kHz), NOT a text input. The
        previous implementation read `#freqinput`, an element that does
        not exist on any websdr.org build -- verified live against the
        real HTML of five instances (Twente, Hack Green, IS0GRB, NA5B,
        Maasbree): the visible frequency box is
        `document.freqform.frequency`, which carries a `name` and no
        `id`. So every verification silently returned None and logged
        "Could not read back #freqinput to verify setfreq()", leaving
        _verify_freq_applied() permanently dead (hundreds of such
        warnings in a real user's log).

        `window.freq` is the right source rather than that text box for
        two independent reasons, both read off websdr-base.js:
          * the box is written as `nomfreq.toFixed(2)` -- rounded to 10
            Hz, so it cannot confirm a sub-10 Hz tune at all; and
          * in CW it shows nominalfreq() (carrier + (hi+lo)/2), whereas
            tune_hz() passes the carrier/BFO frequency as expected_hz --
            comparing against the box would be off by the CW filter
            centre (~750 Hz with the site's default CW filter) and would
            trip the mismatch retry on every single CW tune.
        `freq` is exactly the value setfreq() stores and exactly what
        gets sent upstream as "f=" + freq.toFixed(3), so it is both
        unrounded and mode-independent.
        """
        value = await self._page.evaluate(
            "() => (typeof window.freq === 'number' && isFinite(window.freq)) ? window.freq : null"
        )
        if value is None:
            return None
        try:
            return int(round(float(value) * 1000))
        except (TypeError, ValueError):
            return None

    async def _verify_freq_applied(self, expected_hz: int, pre_push_hz: Optional[int] = None) -> None:
        """pre_push_hz is what the page's own `window.freq` held right
        before we called setfreq(), if known. Without it, a mismatch here
        is ambiguous: it could mean our write never landed (the rig-driven
        case this corrective retune exists for), or it could mean the user
        retuned the page themselves in the roughly FREQ_VERIFY_DELAY_S
        window between our write and this check -- and forcing the page
        back onto our value in that second case silently erases a real
        user action while logging a misleading "WebSDR did not apply
        requested frequency" warning that isn't true. When pre_push_hz IS
        known, it disambiguates: if the page still shows (approximately)
        its pre-push value, our write genuinely didn't take and the
        corrective retune below is exactly right; if it shows some THIRD
        value -- neither what we asked for nor where it was before -- then
        something else changed it in the meantime and this backs off
        instead of fighting whoever/whatever that was."""
        try:
            await asyncio.sleep(FREQ_VERIFY_DELAY_S)
            if not self._attached:
                return
            actual_hz = await self._read_page_freq_hz()
            if actual_hz is None:
                logger.warning("Could not read back window.freq to verify setfreq()")
                return
            if abs(actual_hz - expected_hz) <= FREQ_VERIFY_TOLERANCE_HZ:
                return

            if pre_push_hz is not None and abs(actual_hz - pre_push_hz) > FREQ_VERIFY_TOLERANCE_HZ:
                logger.debug(
                    "Skipping corrective re-tune: page now shows %.3f kHz, matching neither "
                    "the requested %.3f kHz nor its %.3f kHz pre-push value -- treating as a "
                    "user-initiated change, not a failed write.",
                    actual_hz / 1000, expected_hz / 1000, pre_push_hz / 1000,
                )
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
            # Same silent USB<->LSB flip risk as tune_hz()'s setband() call --
            # see that call site's comment.
            if self._current_web_mode is not None:
                await self._page.evaluate("(m) => window.set_mode(m)", self._current_web_mode)
            await self._page.evaluate("(khz) => window.setfreq(khz)", expected_hz / 1000.0)

            await asyncio.sleep(FREQ_VERIFY_DELAY_S)
            actual_hz = await self._read_page_freq_hz()
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
            if hamlib_mode != self._last_unmapped_mode:
                self._last_unmapped_mode = hamlib_mode
                logger.warning(self._last_mode_error)
            else:
                logger.debug(self._last_mode_error)
            return False

        try:
            await self._page.evaluate("(m) => window.set_mode(m)", web_mode)
            self._current_web_mode = web_mode
            # base mode without the "N" (narrow) suffix, used e.g. to detect CW for cw_offset_hz
            self._current_mode = web_mode[:-1] if web_mode.endswith("N") else web_mode
            self._last_mode_error = None
            self._last_unmapped_mode = None
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

    def _reverse_effective_hz(
        self, observed_hz: Optional[int], observed_hamlib_mode: Optional[str]
    ) -> Optional[int]:
        """Un-applies cw_offset_hz for the reverse direction (WebSDR ->
        rig), symmetric to tune_hz()'s forward application above. Takes
        the mode as an explicit argument, sourced from the SAME status
        snapshot map_websdr_mode_to_hamlib() derived it from -- NOT
        self._current_mode, which only reflects this driver's own last
        PUSHED mode and is stale by construction for reverse sync (a page
        change the driver didn't itself push is exactly what reverse sync
        exists to observe)."""
        if observed_hz is None:
            return None
        if observed_hamlib_mode == "CW":
            return observed_hz - self.cw_offset_hz
        return observed_hz

    def hamlib_mode_from_status(self, status: WebSDRStatus) -> Optional[str]:
        return map_websdr_mode_to_hamlib(status.mode)

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
                "() => ({freq: window.freq, mode: window.mode, "
                "audio: document.ct ? document.ct.state === 'running' : null})"
            )
            # Displayed mode is normalized to its base (non-narrow) form --
            # window.mode can legitimately be e.g. "USBN" for a
            # narrow-filter USB selection, which reads as a different mode
            # than the hamlib USB it actually corresponds to if shown raw
            # (mirrors set_mode()'s own self._current_mode stripping above).
            raw_mode = data.get("mode")
            mode = raw_mode[:-1] if isinstance(raw_mode, str) and raw_mode.endswith("N") else raw_mode
            return WebSDRStatus(
                connected=True,
                freq_hz=int(round(float(data["freq"]) * 1000)) if data.get("freq") is not None else None,
                mode=mode,
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
