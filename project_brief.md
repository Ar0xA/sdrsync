# sdrsync — project brief

_Last updated: 2026-08-06 (v3 round: KiwiSDR driver + WebSDR type auto-detection, plus the pa3fwm→websdr_org rename)_

## What this is

Open-source Python alternative to CatSyncSDR. Keeps a WebSDR (a shortwave
receiver tuned through a website) in sync with a real transceiver over the
network via hamlib's `rigctld` (not Omnirig).

**Sync direction is one-way only: transceiver → WebSDR.** Turning the
dial/mode on the rig retunes the WebSDR; the reverse is explicitly not
implemented (deliberate choice, not a gap).

Full design rationale and block-by-block plan (now covers both the original
build and this v2 robustness/feature round):
`C:\Users\ABEL75\.claude\plans\rippling-roaming-forest.md`

## How it works (approach)

Rather than reimplementing the WebSDR's custom binary websocket/audio
protocol, the app launches a real Chromium tab via Playwright pointed at the
actual WebSDR page, and drives it by calling the site's own JS functions
(`window.setfreq(khz)`, `window.set_mode(str)`, `window.setband(idx)`,
`window.soundapplet.mute(0/1)`) — confirmed by reading the live site's
actual JS source, not guessed. This gets real audio playback for free
through the real browser tab — **including when the window is hidden**
(v2's `AppSettings.headless` option). That option does *not* use Chromium's
real `--headless` mode (confirmed by testing: it has no audio output at
all, on any binary/flag combination tried so far — see "Known issues" for
the still-open investigation into why). It instead launches a normal headed
browser positioned off-screen, which keeps real audio working. See "Known
issues to debug further" below before assuming headless-with-audio is a
solved/understood problem at the Chromium level — only the app-level
workaround is confirmed, not the deeper question.

GUI is Tkinter (stdlib only): WebSDR site dropdown, WebSDR connection/freq/
mode/audio status, transceiver (rigctld) connection/freq/mode/PTT status,
a Test-Connection preflight button, headless/mock-rig checkboxes, and (v2)
a live Mock Rig Control panel shown only in mock mode.

## Status: v1 + v2 + v3 implementation complete, still untested against real hardware

v1 (7 blocks, A–G) shipped first: config/persistence, rigctld client +
fake_rigctld dev server, WebSDR driver interface + PA3FWM driver, the
one-way sync engine, the Tkinter GUI, unit tests, packaging/docs.

v2 added 6 more blocks (H–M) on top, per a follow-up request for broader
error handling, degraded-mode resilience, and a hardware-free testing loop:

| Block | What | File(s) |
|---|---|---|
| H. Preflight | "Test Connection" button — TCP+HTTP reachability, no browser launch | `preflight.py` |
| I. Graceful degradation | WebSDR attach failure no longer kills rig monitoring; persistent attach-retry-with-backoff task runs alongside the poll loop | `websdr/pa3fwm.py`, `sync/engine.py` |
| J. Visible mode errors | Unsupported hamlib mode now shows in the GUI, not just the log | `websdr/pa3fwm.py` |
| K. Headless option | Opt-in headless Chromium (no audio) | `config.py`, `sync/engine.py`, `gui/app.py` |
| L. Embedded mock rig | GUI checkbox spins up `fake_rigctld` in-process; live Mock Rig Control panel (freq/mode/PTT) only shown in mock mode | `rig/fake_rigctld.py`, `sync/engine.py`, `gui/app.py` |
| M. Error-handling audit | Wrapped `SyncEngine` construction; fixed a GUI shutdown race | `gui/app.py` |

v3 added 4 more blocks (N–Q) per a follow-up request to support a second
WebSDR software family (KiwiSDR) and auto-detect which family a pasted URL
runs, plus renamed the original driver from `pa3fwm` to `websdr_org`:

| Block | What | File(s) |
|---|---|---|
| N. KiwiSDR driver | New driver for the KiwiSDR software family (`window.ext_tune`/`ext_get_freq_kHz`/`ext_get_mode`/`ws_snd`/`toggle_or_set_mute`), same `_attached`-gating and bool-return discipline as websdr_org | `websdr/kiwisdr.py` |
| O. Fingerprinting | `FINGERPRINT_MARKERS` class attr on each driver; `detect_driver_type(html)` scans `<script src>` tags only, returns `None` on zero or multiple matches (never guesses) | `websdr/base.py`, `websdr/registry.py`, `preflight.py` (`detect_websdr_type`) |
| P. GUI Custom URL + Detect | "Custom URL..." dropdown entry + URL field + Detect button; `_resolve_selected_site()` replaces a silent-fallback-to-Twente bug; `last_site_driver_type` persists a custom site across restarts; `GuiMessage` base + dispatch-table refactor of `_poll_status_queue` | `config.py`, `gui_messages.py` (new), `preflight.py` (`DetectResult`), `sync/engine.py` (`StatusSnapshot` now a `GuiMessage`), `gui/app.py` |
| Q. Tests + docs | `test_kiwisdr_mode_mapping.py`, `test_fingerprint.py`; README + this file updated | `tests/`, `README.md`, `project_brief.md` |

Also: the original driver module/class was renamed `pa3fwm.py`/`PA3FWMDriver`
→ `websdr_org.py`/`WebsdrOrgDriver` throughout (driver, tests, registry,
`KNOWN_SITES`), since "PA3FWM" was the software's author's callsign, not a
name a new user would recognize — "websdr.org" (the software's actual
project site) is the more discoverable name for the `driver_type` string
and module name. No behavior change.

Prior single-file prototypes (`websdrSync.py`, `websdrSync1.1.py`,
`websdrSync.1.2.py`) are still in the repo root — superseded by the
`sdrsync/` package, safe to delete once the package is confirmed working
against real hardware, or keep as reference.

### Three review passes done so far
1. Opus reviewed the **v1 plan** before coding (band-switching, autoplay
   gating, thread-safety contract, debounce/rate-cap).
2. Opus reviewed the **v1 implementation** and found 11 real bugs (thread
   races, a leaked socket, stale-frequency-after-CW-switch, etc.) — all
   fixed.
3. Opus reviewed the **v2 plan**, catching that the original design for
   graceful WebSDR-down degradation rested on a false premise (driver
   methods were *not* actually safe no-ops during an attach retry, since
   `attach()` sets `self._page` before it does anything else) — the plan
   was revised before any v2 code was written. Then Opus reviewed the **v2
   implementation** and found several more real bugs, all fixed:
   - `attach()` didn't reset stale `_current_band`/`_current_mode` on
     reconnect, and never cleared `_last_page_error`/`_last_tune_error`,
     so the GUI could show a permanently stuck error after a full recovery.
   - A non-`PlaywrightError` exception from `attach()` (e.g. malformed page
     JSON) would silently kill the attach-retry background task forever.
   - The embedded mock rigctld server could leak a bound port if the
     browser failed to launch right after it started.
   - **The most subtle one**: `tune_hz`/`set_mode` used to be awaited but
     their result ignored, so a call that silently no-op'd (driver not yet
     attached) or failed got unconditionally recorded as "sent" — meaning
     a WebSDR outage could permanently desync frequency/mode until the rig
     physically moved again, even after the WebSDR came back. Fixed by
     making both methods return `bool` ("did this actually apply?") and
     having the engine only update its dedupe bookkeeping on `True`. Now
     covered by a regression test (`test_failed_push_is_retried_not_latched_as_sent`).
   - The Mock Rig Control panel could show/hide based on the *live
     checkbox* instead of the actually-running engine's setting, so
     toggling the checkbox mid-session could show mock controls during a
     real-rig run (or hide them during a real mock run). Fixed by gating
     on `engine.settings.use_mock_rig` and disabling the checkboxes while
     connected.
   - Duplicate shutdown-polling chains possible between a user-clicked
     Disconnect and an engine-reported fatal error arriving around the
     same time. Fixed with a `_stopping` guard flag.
4. Opus reviewed the **v3 plan** before coding, catching three issues fixed
   before implementation: (a) the originally-drafted Custom-URL GUI flow
   had the same silent-fallback-to-Twente bug as an existing (latent, then
   unreachable) one in `_start_engine`/`_on_test_clicked` — fixed by adding
   `_resolve_selected_site() -> Optional[WebSDRSite]` with no fallback; (b)
   the plan's premise "KiwiSDR has no band table so no PA3FWM-style
   silent-drop failure mode, skip frequency-readback verification" was
   backwards — Kiwi has *worse* silent-drop risks (public instances can
   drop `ws_snd` from user-count/time limits; admin-configured frequency
   ranges can silently clamp/reject) — so `tune_hz`/`set_mode` verify via
   readback just like websdr_org; (c) `last_site_url` already silently
   reverted to Twente on restart if the saved URL wasn't in `KNOWN_SITES` —
   closed by adding `last_site_driver_type` so a Custom URL site
   reconstructs correctly on restart instead of reverting.
5. Opus reviewed the **v3 implementation** and found 4 real bugs, all fixed:
   - `kiwisdr.py`'s narrow-mode table derived `usbn`/`lsbn` via a generic
     `+ "n"` suffix rule, but KiwiSDR's actual narrow-variant strings
     (confirmed from the site's own `passbands_fallback` table, already
     documented in the driver's own docstring) are `usn`/`lsn` — so narrow
     USB/LSB modes were silently rejected by the page forever, with the
     engine retrying `set_mode` every poll tick and the GUI showing a
     permanent error. Fixed by making the narrow-variant string an
     explicit table entry per mode instead of derived.
   - `tune_hz`'s CW-offset check compared `self._current_mode == "cw"`,
     but `set_mode` stores the full mode string including its narrow
     suffix (`"cwn"` for any passband under 2000 Hz — i.e. almost every
     real CW setup), so the comparison never matched and `cw_offset_hz`
     was silently never applied. Fixed with a `_base_mode_of()` helper
     that strips the narrow/wide suffix before comparing.
   - The GUI's Detect flow paired the detected `driver_type` with
     whatever URL happened to be in the text field when the background
     check *returned*, not the URL it was actually run against — editing
     the field while a Detect was in flight could silently attach the
     wrong driver to the wrong URL. Fixed by adding a `url` field to
     `DetectResult` and discarding a result whose URL no longer matches
     the field.
   - `_apply_detect_result` unconditionally re-enabled the Detect button
     regardless of the current combobox selection or connected state,
     which could show a live Detect button next to a greyed-out URL field
     (after switching to a known site mid-detect) or while already
     connected (after switching to a known site, connecting, and having a
     stale detect result arrive late). Fixed by routing through the same
     `_update_custom_url_visibility()` state machine everything else uses,
     gated on `self.engine is None`.
   Also noted but left as-is (documented, not fixed): a rejected/clamped
   KiwiSDR frequency causes `tune_hz` to retry every poll tick (~200ms)
   indefinitely rather than backing off, since `_last_sent_freq` never
   latches on a `False` return — cosmetic/bandwidth cost only, no
   correctness impact, and the same shape already exists implicitly
   elsewhere in the engine's retry design.

### What's been verified so far
- `pytest` — 39/39 passing (pure-logic + stubbed-engine tests: mode
  mapping, band selection, rigctld response parsing, mode/frequency-sync
  independence, and the new failed-push-is-retried regression test; no
  browser/hardware needed).
- Manual smoke test (v1): `RigctldClient` against `fake_rigctld.py`'s
  server — connect, get_freq/get_mode/get_ptt, live state changes.
- **New end-to-end integration smoke test (v2)**: embedded mock rig + a
  real headless Chromium browser + a stub WebSDR driver (swapped in for
  PA3FWMDriver to avoid needing the real WebSDR site) — confirmed
  frequency/mode/PTT pushed via the mock-rig thread-safe setters actually
  reach the driver, and the mock server is fully torn down
  (`engine._mock_server is None`) after a clean `stop_from_other_thread()`
  shutdown. This did **not** exercise the real `PA3FWMDriver`'s
  `attach()`/retry path or any Tkinter code — those still need a real
  browser session / manual GUI click-through respectively.
- Playwright's Chromium browser is installed
  (`C:\Users\ABEL75\AppData\Local\ms-playwright\`).
- The GUI surfaces background-thread crashes (missing Chromium, mock-rig
  port conflicts, etc.) as readable messages instead of a bare traceback,
  and cleanly re-enables Connect once the background thread actually exits.

## Known issues to debug further

Found during the first real click-through of the GUI (mock rig + real
headed Chromium against the live `websdr.ewi.utwente.nl:8901` site,
2026-08-06 ~15:07). App ran end-to-end without crashing (Connect, WebSDR
attach, Test button, clean Disconnect — see log excerpt below), but:

1. **`#freqinput` element not found during frequency verification.**
   Log showed, twice:
   ```
   [WARNING] sdrsync.websdr.pa3fwm: Could not read back #freqinput to verify setfreq()
   ```
   This is `PA3FWMDriver._read_freqinput_hz()` in `websdr/pa3fwm.py`
   (`document.getElementById('freqinput')`) failing to find that element on
   the real page, ~2s and ~18s after a `setfreq()` call. Non-fatal — the
   actual `window.setfreq()` call still fires regardless, so tuning itself
   isn't necessarily broken — but it silently defeats the
   "detect-a-stuck/failed-tune-and-retry" safety net in
   `_verify_freq_applied()`, since it can never tell a real mismatch from
   "couldn't find the field." Needs investigating against the live page: is
   `#freqinput` the wrong element ID for this specific WebSDR instance /
   this version of the PA3FWM software, is it present but not yet rendered
   at verification time (race with `FREQ_VERIFY_DELAY_S = 0.6s`), or
   something else? Worth inspecting the live DOM directly (e.g. via the
   Browser pane tool) rather than guessing.

2. **Headless + audible output — shipped workaround confirmed working; the
   underlying "can true headless ever have real audio" question is still
   open and explicitly parked for later** (user: "guess we dig into it
   later").

   **What's shipped and confirmed working (do not need to revisit this
   part):** `sync/engine.py` no longer passes `headless=True` to Playwright
   at all when `AppSettings.headless` is True. Instead it launches a
   normal **headed** browser (full, real audio pipeline) and passes
   `--window-position=-32000,-32000` so the window renders far off any
   screen and is effectively invisible. GUI checkbox relabeled "Hide
   browser window (audio still plays)". `config.py`'s `headless` field
   keeps its name for config-file backwards-compatibility; its docstring
   explains the real mechanism. **Confirmed by a human listening** that
   audio is audible with this approach.

   **What's still an open question (the "dig into it later" part):**
   whether Chromium's *actual* `--headless` mode can ever be made to
   produce real, audible system output, on any flag combination. Three
   attempts so far, all connected fine but **produced no audible sound**,
   despite the page's `AudioContext.state` reporting `"running"` (i.e. the
   JS-side pipeline believes it's playing):
   1. `headless=True` with Playwright's default lightweight "headless
      shell" binary.
   2. `headless=True` forced onto the full Chromium binary via
      `channel="chromium"`.
   3. Explicit `--headless=new --autoplay-policy=no-user-gesture-required
      --no-mute-audio` on the full binary directly (bypassing Playwright's
      `headless=` param, passing `--headless=new` as a raw launch arg
      instead) — this was tried specifically because the user had been
      told `--no-mute-audio` + explicit `--headless=new` (not old/shell)
      "should work"; installed Chromium is version **151.0.7922.34**
      (confirmed via `chrome.exe --version`), well past the "Chrome 112+"
      threshold sometimes cited for new-headless audio support, so version
      age is not the blocker here. Still silent.

   Working theory (not yet proven either way): Chromium's headless mode —
   old or new, either binary — instantiates a stub/fake audio output
   backend internally that never attaches to a real platform audio device,
   independent of autoplay-policy or mute-audio flags, which only control
   whether the *page* is allowed/willing to play audio, not whether
   headless mode has a real output sink to play it *through*. Standalone
   test script used for attempt 3, kept for reuse:
   `C:\Users\ABEL75\AppData\Local\Temp\claude\C--Users-ABEL75-OneDrive-HAM-sdrsync\187ec702-3587-4416-af2b-7927f63eb95f\scratchpad\test_headless_audio_flags.py`
   (loads the live WebSDR page directly with given launch args, tunes it,
   holds for 20s for a human to listen, prints `AudioContext.state`).

   **Ideas not yet tried, for the next session:**
   - A virtual audio device (e.g. VB-Audio Virtual Cable) as the Windows
     default output — some headless-audio automation setups rely on
     routing through a virtual sink rather than expecting headless
     Chromium to talk to a real physical device directly; worth testing
     whether headless mode *can* attach to a virtual device even though it
     apparently won't attach to the real one.
   - Explicitly check (e.g. via `chrome://media-internals` in a headed
     window, or Chromium's `--enable-logging --v=1` audio service logs)
     whether the audio *service* itself starts in headless mode at all, to
     confirm/refute the stub-backend theory with actual evidence rather
     than inference.
   - Chromium source/issue tracker search for "headless audio output
     device" / "AudioOutputStream" in the headless embedder code, to get a
     definitive yes/no instead of empirical guessing.

   Given the off-screen-headed-window workaround already fully solves the
   practical goal (invisible + audible), this is a "nice to know" /
   correctness-of-understanding thread, not a blocker for the app.

3. **KiwiSDR audio-gate / AudioContext-state uncertainty (v3, not yet
   live-tested).** `KiwiSDRDriver` was written and reviewed against the
   real site's JS source and confirmed live for its *control* functions
   (`ext_tune`, `ext_get_freq_kHz`, `ext_get_mode`, `ws_snd.readyState`,
   `toggle_or_set_mute`) the same way `websdr_org.py` was — but unlike
   `websdr_org.py`, no confirmed `AudioContext`-equivalent global was found
   on the Kiwi page to do a PA3FWM-style "check state, resume if suspended"
   step. `get_status()`'s `audio_active` field is therefore documented as
   proxying `ws_snd.readyState === 1` ("is the sound socket open"), not a
   true "is audio actually playing" signal — a page that's stuck on
   autoplay-block could still report `audio_active: true` here. Needs a
   real headed-browser session against a live KiwiSDR instance to confirm
   whether audio actually plays out of the box (Kiwi's own click-to-start
   UI may already satisfy the autoplay gate as part of `ws_snd` reaching
   `readyState 1`, in which case this is a non-issue) or whether a
   corner-click / explicit resume step needs adding, mirroring
   `_satisfy_audio_gate()` in `websdr_org.py`.

4. **First real click-through of the KiwiSDR driver (2026-08-06) found and
   fixed two real bugs, plus confirmed one target site has an unfixable
   bug of its own:**
   - **Fixed — `attach()` readiness race.** The `wait_for_function` gate
     only checked `ws_snd.readyState === 1` (sound socket open), but the
     page's own default demodulator object isn't created until slightly
     *after* that (its own async init callback) — calling `ext_tune()`
     before it exists throws inside the site's own JS
     (`demodulator_set_offset_frequency`: "Cannot set properties of
     undefined"). Confirmed empirically (`demodulators.length` goes 0→1
     roughly 1-3s after `readyState` hits 1) against a live instance
     (`kiwisdr.areg.org.au:8073`, picked fresh from `kiwisdr.com/public/`).
     Fixed by adding `typeof demodulators !== 'undefined' &&
     demodulators.length > 0` to the same `wait_for_function` condition —
     re-tested against the same instance afterward: `tune_hz()` and
     `set_mode()` both now return `True` immediately post-attach, no sleep
     needed.
   - **Fixed — `get_status()` crashed on a transient NaN frequency
     reading.** `ext_get_freq_kHz()` can legitimately return the string
     `"nan"` right after attach (before any tune has landed yet) — Python's
     `int(round(float("nan")*1000)))` raises `ValueError: cannot convert
     float NaN to integer`, which was only caught by the broad
     `except (PlaywrightError, TypeError, ValueError)` around the *whole*
     status read, so it flipped `self._attached = False` and made the
     driver think it had lost the page entirely — which then made
     `tune_hz`/`set_mode` return `False` immediately (gated on
     `self._attached`) even though the page was perfectly fine. Fixed by
     explicitly checking `math.isnan()` and treating it as "frequency not
     known yet" (`freq_hz=None`) rather than letting it raise.
   - **Confirmed, not fixable from our side — the example site in
     `KNOWN_SITES` (`23126.proxy.kiwisdr.com:8073`) has its own bug.**
     After both fixes above, `tune_hz()` (frequency-only) works fine
     against it, but `set_mode()` always throws inside the site's own
     `kiwi_passbands()`: `TypeError: Cannot read properties of undefined
     (reading 'usb')`, because that instance's `cfg.passbands` global is
     `undefined` (the site's own code does `cfg.passbands[mode]` with no
     null-check). This is specific to that instance's server-side config,
     not a driver bug — the same code path worked cleanly against
     `kiwisdr.areg.org.au:8073`. Confirmed non-fatal to the app either way:
     `set_mode()` catches the `PlaywrightError`, logs it, returns `False`,
     and the engine just keeps retrying next time the rig's mode changes
     rather than crashing anything; frequency sync is unaffected since it
     doesn't share this code path. **If the example site keeps having this
     issue, consider swapping `KNOWN_SITES`' KiwiSDR entry for a
     less-flaky instance** — pick one from `kiwisdr.com/public/` (click
     through the captcha-style "Click to show KiwiSDRs" splash first).

Log excerpt from that run (full log at
`C:\Users\ABEL75\.sdrsync\sdrsync.log`):
```
15:07:20 [INFO] Fake rigctld listening on 127.0.0.1:4532
15:07:23 [INFO] Sync engine started (rig -> WebSDR, one-way)
15:07:23 [INFO] Client connected: ('127.0.0.1', 53455)
15:07:24 [INFO] Loaded 1 band(s) from http://websdr.ewi.utwente.nl:8901/
15:07:24 [WARNING] [WebSDR page console:warning] The ScriptProcessorNode is
         deprecated. Use AudioWorkletNode instead. (harmless, site's own code)
15:07:26 [WARNING] Could not read back #freqinput to verify setfreq()
15:07:42 [WARNING] Could not read back #freqinput to verify setfreq()
15:07:57 [INFO] Client connected: ('127.0.0.1', 56169)   <- Test button click
15:07:58 [INFO] Client disconnected: ('127.0.0.1', 56169)
15:08:00 [INFO] Client disconnected: ('127.0.0.1', 53455)  <- clean shutdown
```

### What has NOT been verified yet
- End-to-end with a **real transceiver + real rigctld** — no hardware was
  available in the environment this was built in. This is the main
  remaining item: run `python -m sdrsync.main`, point it at a real rigctld
  instance, and confirm turning the rig's dial/mode actually retunes the
  live `websdr.ewi.utwente.nl:8901` browser tab and the GUI's transceiver
  panel.
- The actual **Tkinter GUI has never been click-tested** in this
  environment (no display automation available for a native desktop app
  here) — the v2 widgets (Test button, headless checkbox, mock-rig
  checkbox + Mock Rig Control panel, checkbox disabling while connected)
  are verified by code reading + the non-GUI integration smoke test, not
  by actually clicking them.
- The real `PA3FWMDriver.attach()`/attach-retry path against the *actual*
  live WebSDR site (band table loading, audio gate, freq verification) —
  the v2 integration smoke test used a stub driver instead, precisely to
  avoid depending on the real site being reachable during dev. Worth one
  manual real-site run to confirm attach still works after the v2 changes
  to `attach()` (band/mode reset, broadened exception handling).
- The graceful-degradation behavior specifically: manually blocking the
  WebSDR URL (or killing rigctld) mid-run to watch the *other* side keep
  working and the attach-supervisor recover automatically once unblocked.
- **v3, not yet click-tested at all**: `KiwiSDRDriver.attach()`/tune/mode
  against the real live KiwiSDR example site (`http://23126.proxy.kiwisdr.com:8073/`)
  — verified only by reading + calling functions manually in a research
  browser tab during driver development, not by an actual mock-rig-driven
  GUI session; the Custom URL + Detect flow end-to-end (paste a URL, click
  Detect, confirm the right driver_type is identified and Connect reaches
  that site, not a fallback); and restart-persistence of a Custom URL site
  via `last_site_driver_type`.

### Easiest way to try it all without hardware
Check **Use mock rig (embedded, for testing)** in the GUI before clicking
Connect — no separate terminal needed anymore (v2). Once connected, a
**Mock Rig Control** panel appears with frequency/mode+passband/PTT
controls that drive the (real, headed unless you also check headless)
WebSDR browser tab live. `fake_rigctld.py`'s standalone terminal mode
(`python -m sdrsync.rig.fake_rigctld`) still works too, if preferred.

## Process hygiene note (why this file exists)

While testing, a stray `python -m sdrsync.rig.fake_rigctld` process was
found still running (holding port 4532) after an earlier session ended —
it has been killed. **After a system restart, nothing should be running**;
if `rigctld` doesn't connect on first try, check for lingering processes:

```powershell
Get-Process | Where-Object { $_.ProcessName -match 'chrome|python' }
```

If audio is heard with no sdrsync/python/Playwright-Chromium process
running, it's not this app — check Windows' Volume Mixer for the real
source (likely a separate, manually-opened browser tab to the WebSDR site).

## Next steps after restart

1. `cd` into the project, confirm `pip install -r requirements.txt` deps
   are still present (they're in the normal Python 3.12 user site-packages,
   should survive a restart).
2. `pytest` to confirm the 39 tests still pass after the restart.
3. Smoke test without hardware first: `python -m sdrsync.main`, check
   **Use mock rig**, Connect, try the Mock Rig Control panel and the
   **Test** button.
4. Then test against real hardware: uncheck mock rig, start real
   `rigctld -m <model> -r <port>`, launch `python -m sdrsync.main`, select
   the Twente site, Connect, and verify the DoD — changing freq/mode on the
   rig changes the WebSDR.
5. **v3-specific**: select the KiwiSDR example site (or paste its URL under
   Custom URL and click Detect), Connect with mock rig, and confirm the
   live KiwiSDR tab follows frequency/mode/PTT the same way websdr_org
   does — this driver has only been checked by direct JS calls in a
   research tab, never through the actual app. Also try Custom URL with an
   unrelated (non-WebSDR) URL to confirm Detect reports "not recognized"
   rather than guessing.
6. If anything misbehaves, `sdrsync.log` under
   `C:\Users\ABEL75\.sdrsync\sdrsync.log` has full structured logs
   (console + file, per `logging_setup.py`).
7. Also still open: the `#freqinput` verification warning and the KiwiSDR
   audio-gate/AudioContext-state uncertainty (items 1 and 3 under "Known
   issues"), and, if wanted, the deeper headless-audio investigation
   (item 2's "still open" half) — none block normal use, all are
   candidates for the next session if there's time.

v3's implementation review (Opus, 4 real bugs found and fixed — see "Three
review passes" above, now four) is complete; v3 is code-complete pending
the manual real-hardware/real-KiwiSDR smoke tests listed above.

Current `C:\Users\ABEL75\.sdrsync\config.json` was left with `use_mock_rig:
true` and `headless: true` from this session's testing — reset those (via
the GUI checkboxes, or edit the file) before a real-hardware run if they're
still set that way.
