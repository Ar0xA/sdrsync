"""Configuration: known WebSDR sites and persisted user settings."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("sdrsync.config")

CONFIG_DIR = Path.home() / ".sdrsync"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass(frozen=True)
class WebSDRSite:
    """A selectable WebSDR instance and which driver knows how to control it."""
    name: str
    url: str
    driver_type: str


# Starter list for the dropdown. Add more sites here as they're supported;
# driver_type must match a key in sdrsync.websdr.registry.DRIVERS.
#
# NOTE: these are all third-party public receivers, not ours -- they can go
# offline, move, or (as already happened once with the original KiwiSDR
# entry, which had a server-side bug breaking set_mode()) develop instance-
# specific quirks. Treat this list as needing occasional revalidation, not
# a permanent fixture.
KNOWN_SITES: list[WebSDRSite] = [
    WebSDRSite(
        name="Twente wide-band (websdr.org)",
        url="http://websdr.ewi.utwente.nl:8901/",
        driver_type="websdr_org",
    ),
    WebSDRSite(
        name="KiwiSDR example (VK5ARG)",
        url="http://kiwisdr.areg.org.au:8073/",
        driver_type="kiwisdr",
    ),
    WebSDRSite(
        # 80m HF band (~2.65-4.65 MHz), confirmed live -- the previous
        # example (sdr2.justjakob.de) was VHF-only (2m, ~144-146 MHz),
        # which made every ordinary HF rig frequency report as "out of
        # range" by default. OpenWebRX instances with true full-HF
        # (0-30 MHz) single-profile coverage are uncommon (most consumer
        # SDR hardware this software runs on doesn't have that much
        # instantaneous bandwidth), so this is a deliberately narrower but
        # actually-useful-for-HF-testing default rather than a wideband one
        # like the websdr.org/KiwiSDR entries above.
        name="OpenWebRX example (OH6KK, 80m)",
        url="http://oh6kk.dy.fi:8073/",
        driver_type="openwebrx",
    ),
]


@dataclass
class AppSettings:
    """User-editable settings, persisted as JSON in the user's home dir."""
    rigctld_host: str = "127.0.0.1"
    rigctld_port: int = 4532
    last_site_url: str = field(default_factory=lambda: KNOWN_SITES[0].url)
    # Set alongside last_site_url so a Custom URL site (not present in
    # KNOWN_SITES) can be reconstructed on restart instead of silently
    # reverting to KNOWN_SITES[0]. Empty string means "not a custom site".
    last_site_driver_type: str = ""
    # WebSDR sites the user saved to the dropdown via the "Save to list"
    # button (a proven-working Custom URL), each dict:
    # {"name": str, "url": str, "driver_type": str}. Separate from
    # KNOWN_SITES, which are the app's built-in defaults and aren't
    # user-editable.
    user_sites: list = field(default_factory=list)
    cw_offset_hz: int = 0
    mute_on_tx: bool = True
    # Despite the name, this does NOT use Chromium's real --headless mode --
    # that has no audio output at all (confirmed: neither the lightweight
    # headless-shell binary nor the full binary's own headless mode play
    # sound). Instead sync/engine.py launches a normal headed browser
    # positioned far off-screen when this is True, keeping real audio intact
    # while showing no visible window.
    headless: bool = False
    use_mock_rig: bool = False

    @classmethod
    def load(cls) -> "AppSettings":
        if not CONFIG_FILE.exists():
            return cls()
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            known_fields = {f for f in cls.__dataclass_fields__}
            filtered = {k: v for k, v in data.items() if k in known_fields}
            return cls(**filtered)
        except (json.JSONDecodeError, OSError, TypeError) as e:
            logger.warning("Could not load %s (%s); using defaults", CONFIG_FILE, e)
            return cls()

    def save(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning("Could not save settings to %s (%s)", CONFIG_FILE, e)


def find_site_by_url(url: str) -> WebSDRSite | None:
    for site in KNOWN_SITES:
        if site.url == url:
            return site
    return None
