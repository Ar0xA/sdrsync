"""Maps a WebSDRSite.driver_type string to its driver class.

Add a new WebSDR software family by implementing sdrsync.websdr.base.WebSDRDriver
in its own module (including a FINGERPRINT_MARKERS class attribute) and
registering it here.
"""
from __future__ import annotations

import re
from typing import Optional

from sdrsync.websdr.kiwisdr import KiwiSDRDriver
from sdrsync.websdr.openwebrx import OpenWebRXDriver
from sdrsync.websdr.websdr_org import WebsdrOrgDriver

DRIVERS = {
    "websdr_org": WebsdrOrgDriver,
    "kiwisdr": KiwiSDRDriver,
    "openwebrx": OpenWebRXDriver,
}

_SCRIPT_SRC_RE = re.compile(r'<script[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def detect_driver_type(html: str) -> Optional[str]:
    """Guess a driver_type from a WebSDR site's raw root-page HTML.

    Scoped to <script src="..."> attribute values only, not the whole HTML
    body -- station-description text or chat could otherwise mention a
    driver's filename and produce a false positive. If more than one
    registered driver's markers match, returns None ("ambiguous, refuse to
    guess") rather than picking one -- DRIVERS dict ordering must never
    become a load-bearing tie-break rule.
    """
    script_srcs = _SCRIPT_SRC_RE.findall(html)
    matches = [
        driver_type
        for driver_type, driver_cls in DRIVERS.items()
        if any(
            marker in src
            for src in script_srcs
            for marker in getattr(driver_cls, "FINGERPRINT_MARKERS", ())
        )
    ]
    if len(matches) == 1:
        return matches[0]
    return None
