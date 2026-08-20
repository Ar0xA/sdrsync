"""Non-finite values from external rig/page boundaries must be ignored."""
import asyncio

import pytest

from sdrsync.websdr.kiwisdr import KiwiSDRDriver
from sdrsync.websdr.openwebrx import OpenWebRXDriver
from sdrsync.websdr.ubersdr import UberSDRDriver
from sdrsync.websdr.websdr_org import WebsdrOrgDriver


@pytest.mark.parametrize(
    "driver_class",
    (WebsdrOrgDriver, KiwiSDRDriver, OpenWebRXDriver, UberSDRDriver),
)
@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_all_drivers_reject_non_finite_tune_requests_without_touching_page(driver_class, value):
    driver = driver_class("http://example.invalid/")
    driver._attached = True
    driver._page = None

    assert asyncio.run(driver.tune_hz(value)) is False
