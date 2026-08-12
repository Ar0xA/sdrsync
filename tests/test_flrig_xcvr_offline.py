"""flrig's rig.get_vfo/get_mode return FIXED placeholders ("14070000" Hz /
"USB") whenever xcvr_online is false in flrig itself (rig off/unplugged,
or flrig's own XML-RPC toggle disabled) -- confirmed against flrig's real
source, src/server/xml_server.cxx. Without probing rig.get_xcvr (empty
string under that exact same condition), those placeholders are silently
indistinguishable from a genuine reading, and sdrsync would happily sync a
public WebSDR to a fabricated 14.070 MHz USB, or fight the operator's real
tuning via reverse sync's retry ladder repeatedly reverting to it."""
import asyncio

from sdrsync.rig.fake_flrig import start_server
from sdrsync.rig.flrig import FlrigClient, parse_xcvr_online_response


def test_get_state_returns_all_none_when_xcvr_offline():
    async def run():
        handle, state = await start_server(port=0)
        state.xcvr_online = False
        client = FlrigClient("127.0.0.1", handle.port)
        try:
            assert await client.connect() is True
            result = await client.get_state()
            assert result.freq_hz is None
            assert result.mode is None
            assert result.passband_hz is None
            assert result.ptt is None
        finally:
            await client.close()
            handle.close()
            await handle.wait_closed()

    asyncio.run(run())


def test_get_state_reflects_real_readings_once_xcvr_comes_back_online():
    async def run():
        handle, state = await start_server(port=0)
        state.xcvr_online = False
        client = FlrigClient("127.0.0.1", handle.port)
        try:
            assert await client.connect() is True
            offline_result = await client.get_state()
            assert offline_result.freq_hz is None

            state.xcvr_online = True
            state.freq_hz = 7074000
            state.mode = "LSB"
            online_result = await client.get_state()
            assert online_result.freq_hz == 7074000
            assert online_result.mode == "LSB"
        finally:
            await client.close()
            handle.close()
            await handle.wait_closed()

    asyncio.run(run())


def test_set_freq_refuses_when_xcvr_offline():
    async def run():
        handle, state = await start_server(port=0)
        state.xcvr_online = False
        client = FlrigClient("127.0.0.1", handle.port)
        try:
            assert await client.connect() is True
            original_freq = state.freq_hz
            applied = await client.set_freq(7074000, verify_budget_s=0.05)
            assert applied is False
            # Must not even have attempted the write -- state.freq_hz is
            # untouched, not merely "reverted after a failed readback".
            assert state.freq_hz == original_freq
        finally:
            await client.close()
            handle.close()
            await handle.wait_closed()

    asyncio.run(run())


def test_set_mode_refuses_when_xcvr_offline():
    async def run():
        handle, state = await start_server(port=0)
        state.xcvr_online = False
        client = FlrigClient("127.0.0.1", handle.port)
        try:
            assert await client.connect() is True
            original_mode = state.mode
            applied = await client.set_mode("LSB", verify_budget_s=0.05)
            assert applied is False
            assert state.mode == original_mode
        finally:
            await client.close()
            handle.close()
            await handle.wait_closed()

    asyncio.run(run())


def test_parse_xcvr_online_response():
    assert parse_xcvr_online_response("FT-991A") is True
    assert parse_xcvr_online_response("") is False
    assert parse_xcvr_online_response(None) is False
