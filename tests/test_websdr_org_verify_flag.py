"""tune_hz(verify=False) must not arm the delayed corrective re-tune task
(_schedule_freq_verification/_verify_freq_applied) -- this is what stops
sync/engine.py's periodic full-resync of an ALREADY-unchanged frequency
from fighting a genuine, concurrent reverse-sync push landing in the same
~0.6s verification window (v12; see WebSDRDriver.tune_hz's docstring).
verify=True (the default, used for every real frequency change) must keep
scheduling it exactly as before."""
import asyncio

from sdrsync.websdr.websdr_org import WebsdrOrgDriver


class StubPage:
    def __init__(self) -> None:
        self.calls: list = []

    async def evaluate(self, js, *args):
        self.calls.append((js, args))
        return None


def make_driver() -> WebsdrOrgDriver:
    driver = WebsdrOrgDriver("http://example.invalid/")
    driver._attached = True
    driver._bands = [(0, 30_000_000)]
    driver._current_band = 0
    driver._page = StubPage()
    return driver


async def _tune_and_capture(driver: WebsdrOrgDriver, freq_hz: int, verify: bool):
    """Runs tune_hz and reports whether a verify task was scheduled,
    cleanly cancelling it afterward (it sleeps FREQ_VERIFY_DELAY_S before
    doing anything else, so it never actually runs its body here)."""
    ok = await driver.tune_hz(freq_hz, verify=verify)
    task = driver._verify_task
    scheduled = task is not None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return ok, scheduled


def test_verify_true_schedules_corrective_verification():
    driver = make_driver()
    ok, scheduled = asyncio.run(_tune_and_capture(driver, 14_074_000, verify=True))
    assert ok is True
    assert scheduled is True


def test_verify_false_does_not_schedule_corrective_verification():
    driver = make_driver()
    ok, scheduled = asyncio.run(_tune_and_capture(driver, 14_074_000, verify=False))
    assert ok is True
    assert scheduled is False


def test_verify_defaults_to_true():
    driver = make_driver()
    ok, scheduled = asyncio.run(_tune_and_capture(driver, 14_074_000, verify=True))
    # Sanity: calling tune_hz() with no verify arg at all behaves like verify=True.
    driver2 = make_driver()

    async def _default_call():
        ok2 = await driver2.tune_hz(14_074_000)
        task = driver2._verify_task
        s = task is not None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return ok2, s

    ok2, scheduled2 = asyncio.run(_default_call())
    assert ok is True and scheduled is True
    assert ok2 is True and scheduled2 is True
