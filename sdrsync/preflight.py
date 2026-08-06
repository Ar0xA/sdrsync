"""Lightweight reachability checks for rigctld and the WebSDR site.

Deliberately independent of SyncEngine/Playwright: these are meant to
answer "is it worth clicking Connect?" in under a few seconds, without
launching a browser.
"""
from __future__ import annotations

import asyncio
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from sdrsync.gui_messages import GuiMessage
from sdrsync.websdr import registry

logger = logging.getLogger("sdrsync.preflight")

RIGCTLD_CHECK_TIMEOUT_S = 2.0
WEBSDR_CHECK_TIMEOUT_S = 5.0


@dataclass
class RigPreflightResult(GuiMessage):
    """Published on the same status_queue as StatusSnapshot, from the
    Transceiver panel's own Test button. Scoped to rigctld only -- the rig
    and WebSDR connections are independent subsystems (see SyncEngine), so
    their preflight checks are independent too, not a single combined
    result."""
    ok: bool
    message: str


@dataclass
class WebsdrPreflightResult(GuiMessage):
    """Published on the same status_queue, from the WebSDR panel's own Test
    button. Scoped to the WebSDR URL only -- see RigPreflightResult."""
    ok: bool
    message: str


@dataclass
class DetectResult(GuiMessage):
    """Published on the same status_queue after a Detect click. Carries the
    URL it was run against so the GUI can tell a stale result (the user
    edited the URL field while the check was in flight) from a fresh one,
    rather than pairing the detected driver_type with whatever text
    happens to be in the field when the result arrives."""
    url: str
    driver_type: Optional[str]
    message: str


async def check_rigctld(host: str, port: int, timeout: float = RIGCTLD_CHECK_TIMEOUT_S) -> tuple[bool, str]:
    """Raw TCP connect + optional 'v' (version) query, same shape as
    RigctldClient.connect() but standalone so it doesn't need an engine."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
        return False, f"Could not reach rigctld at {host}:{port} ({e})"

    try:
        writer.write(b"v\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        version = line.decode(errors="replace").strip()
        return True, f"rigctld reachable at {host}:{port}" + (f" ({version})" if version else "")
    except (asyncio.TimeoutError, ConnectionResetError, OSError):
        # Connected but didn't answer 'v' the way we expected -- still
        # reachable at the TCP level, which is most of what matters here.
        return True, f"rigctld reachable at {host}:{port} (TCP connect ok, no version reply)"
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def _http_get_sync(url: str, timeout: float) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            status = resp.status
            if 200 <= status < 400:
                return True, f"WebSDR site reachable ({url}, HTTP {status})"
            return False, f"WebSDR site returned HTTP {status} ({url})"
    except urllib.error.HTTPError as e:
        # Still "reachable" in the sense of DNS/routing/server-is-up; a 4xx
        # from the WebSDR itself is a different problem than being offline.
        if e.code < 500:
            return True, f"WebSDR site reachable ({url}, HTTP {e.code})"
        return False, f"WebSDR site error ({url}, HTTP {e.code})"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"Could not reach WebSDR site {url} ({e.reason if hasattr(e, 'reason') else e})"


async def check_websdr_url(url: str, timeout: float = WEBSDR_CHECK_TIMEOUT_S) -> tuple[bool, str]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _http_get_sync, url, timeout)


def _http_get_body_sync(url: str, timeout: float) -> Optional[str]:
    """Sibling to _http_get_sync that returns the response body instead of
    an (ok, message) tuple -- kept separate so check_websdr_url's existing
    return shape (depended on by Block H's Test button) isn't touched."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


async def detect_websdr_type(url: str, timeout: float = WEBSDR_CHECK_TIMEOUT_S) -> tuple[Optional[str], str]:
    """Fetch a URL's HTML and guess which registered WebSDR driver it needs.

    Deliberately independent of Playwright, like the other preflight
    checks -- detection should be near-instant and not require a browser.
    """
    loop = asyncio.get_running_loop()
    html = await loop.run_in_executor(None, _http_get_body_sync, url, timeout)
    if html is None:
        return None, f"Could not reach {url} to detect its WebSDR type"

    driver_type = registry.detect_driver_type(html)
    if driver_type is None:
        return None, f"Could not identify {url} as a supported WebSDR type"
    return driver_type, f"Detected: {driver_type}"


