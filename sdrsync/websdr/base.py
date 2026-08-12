"""Common interface every WebSDR-software driver implements.

Each WebSDR *software family* (PA3FWM's classic WebSDR, OpenWebRX, KiwiSDR,
...) has an unrelated control API, so each gets its own driver module. The
sync engine and GUI only ever talk to this interface.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Optional, Protocol
from urllib.parse import urlsplit


def same_site(current_url: str, expected_url: str) -> bool:
    """True if two page URLs point at the same site (scheme+host+port+path,
    ignoring query string, fragment, and a trailing slash).

    Used by drivers whose attach() can reuse an already-loaded page
    (engine.py's _switch_websdr(), which navigates the SAME WxPageAdapter
    to a new site rather than tearing the window down) to tell "this is
    still our own site, no need to reload" apart from "a DIFFERENT but
    same-family site happens to already be loaded and ready." Without this
    check, a same-family readiness/handshake probe (OpenWebRX's
    _READY_PREDICATE, UberSDR's agent handshake) passes against ANY live
    instance of that software, not specifically the one just switched to
    -- so attach() would silently skip navigation and every subsequent
    tune/mode/mute call would keep controlling the previous site while the
    GUI shows the new one selected."""
    a, b = urlsplit(current_url), urlsplit(expected_url)
    return (a.scheme, a.netloc, a.path.rstrip("/")) == (b.scheme, b.netloc, b.path.rstrip("/"))


class WebSDRIncompatibleError(Exception):
    """Raised when the live page doesn't expose the control API this driver expects.

    This is the real defense against "the site's JS changed underneath us":
    fail loudly and specifically, rather than reimplementing the site's wire
    protocol (which would be far more brittle to change) or silently no-op'ing.
    """


@dataclass
class WebSDRStatus:
    connected: bool
    freq_hz: Optional[int] = None
    mode: Optional[str] = None
    audio_active: Optional[bool] = None
    last_error: Optional[str] = None


class WebSDRDriver(Protocol):
    """A live connection to one WebSDR instance, driving a real browser tab."""

    # Distinctive strings expected inside a <script src="..."> attribute on
    # the WebSDR's root page HTML, used by websdr.registry.detect_driver_type()
    # to guess a driver_type from an arbitrary URL. Part of the driver
    # contract (not a bolt-on constant) so every new driver declares its own
    # fingerprint alongside its control logic. A tuple, not one string, so a
    # driver can list multiple known marker variants without an API change.
    FINGERPRINT_MARKERS: ClassVar[tuple[str, ...]]

    @property
    def attached(self) -> bool:
        """True once attach() has fully succeeded and stayed healthy since.

        tune_hz/set_mode/set_muted/get_status must all be safe no-ops while
        this is False, so a caller can keep polling other things (e.g. the
        rig) concurrently with an attach-retry loop without special-casing
        "not attached yet" itself."""
        ...

    async def attach(self, page) -> None:
        """Given a freshly-navigated Playwright Page, verify it's a compatible
        WebSDR instance (raise WebSDRIncompatibleError if not) and prepare it
        for control (e.g. satisfy the audio autoplay gate)."""
        ...

    async def tune_hz(self, freq_hz: int, verify: bool = True) -> bool:
        """Set receive frequency, switching band first if necessary.

        Returns True only if actually applied to the page (False if not
        attached, unmapped, or the underlying call failed) -- callers must
        gate any "last sent" bookkeeping on this, not assume success.

        verify=False tells a driver that does its own delayed/background
        readback-and-correct (currently only WebsdrOrgDriver) to skip
        arming it for this call -- used by sync/engine.py's periodic full
        resync, which re-pushes an already-unchanged frequency purely as a
        safety net. Without this, that blind re-push could arm a
        corrective re-tune 0.6s later that fights a genuine, concurrent
        reverse-sync push landing in the same window (confirmed via code
        reading, not yet live-reproduced -- see project_brief.md). Drivers
        that verify synchronously inline (KiwiSDR, OpenWebRX) accept this
        param for interface parity but currently ignore it, since they
        have no separate background task for it to gate."""
        ...

    async def set_mode(self, hamlib_mode: str, passband_hz: Optional[int]) -> bool:
        """Set demodulation mode, mapping from a hamlib mode name.

        Returns True only if actually applied to the page -- see
        tune_hz()'s docstring for why callers must gate on this."""
        ...

    async def set_muted(self, muted: bool) -> None:
        """Mute/unmute the WebSDR's audio (used to silence RX audio while the rig is transmitting)."""
        ...

    async def get_status(self) -> WebSDRStatus:
        """Current status for display in the GUI. Also the source data for
        reverse sync (WebSDR -> rig, v11) -- see hamlib_mode_from_status()/
        rig_freq_from_status() below, fed by this same call's result with
        no extra page round-trip."""
        ...

    def hamlib_mode_from_status(self, status: WebSDRStatus) -> Optional[str]:
        """Reverse-direction (WebSDR -> rig): map this status snapshot's
        already-normalized mode string to a canonical hamlib mode name.
        None if unmapped -- caller should skip the reverse push and log,
        not raise. Pure/sync (no page access) -- status is expected to be
        a value already returned by get_status() in the same tick."""
        ...

    def rig_freq_from_status(self, status: WebSDRStatus) -> Optional[int]:
        """Reverse-direction (WebSDR -> rig): this status snapshot's
        frequency, converted to rig-native Hz (i.e. with cw_offset_hz
        un-applied when hamlib_mode_from_status(status) is 'CW' --
        symmetric to tune_hz()'s forward application). None if the
        status has no usable frequency. Pure/sync, same status-snapshot
        contract as hamlib_mode_from_status()."""
        ...

    async def close(self) -> None:
        ...
