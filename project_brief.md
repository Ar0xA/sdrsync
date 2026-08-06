# SDRSync — project brief

_Last updated: 2026-08-06 (v4 round: OpenWebRX driver, git repo init, project renamed sdrsync -> SDRSync)_

**Catch-up note**: this file was last fully rewritten after v3. Since then,
in the same session: two real bugs found by testing the KiwiSDR driver
against a second live instance (see "v3.5" note below the v3 table), a
GUI redesign decoupling the rig and WebSDR connections into two
independent panels/lifecycles (see "v3.6" note), the project was put under
git (`git init` + initial commit in the project root) and renamed
**SDRSync** for display purposes (window title, README H1) while the
Python package/import path stays lowercase `sdrsync` (renaming that would
be a risky import-path refactor for what was a branding-only request).
Some detail in the earlier sections of this file below predates those
changes and is kept for history rather than corrected line-by-line.

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

**v3.5 — KiwiSDR bug fixes from live click-through (not a numbered
block, done directly against a running app + a fresh live instance)**:
testing the app for real surfaced that the example KiwiSDR site
(`23126.proxy.kiwisdr.com:8073`) has its own server-side bug (`set_mode()`
throws inside the site's own `kiwi_passbands()` because that instance's
`cfg.passbands` is `undefined` — not our bug, confirmed by reading the
site's own JS). Testing against a second, healthy instance
(`kiwisdr.areg.org.au:8073`, picked fresh from `kiwisdr.com/public/`)
found two real bugs in our own driver, both fixed: (1) `attach()`'s
readiness gate only checked `ws_snd.readyState === 1`, but the page's own
demodulator object isn't created until ~1-3s after that, so the very
first `tune_hz`/`set_mode` call could throw inside the page — fixed by
also waiting for `demodulators.length > 0`; (2) `get_status()` crashed on
a transient `"nan"` frequency reading right after attach (before any tune
landed), which incorrectly flipped `_attached` to `False` and made every
subsequent call a no-op — fixed by treating `NaN` as "not known yet"
instead of raising. `KNOWN_SITES`' KiwiSDR entry was later swapped to the
healthy instance in the v4 round (see below) since the example site issue
persists.

**v3.6 — decoupled rig/WebSDR connection lifecycles (GUI + engine
redesign, user-requested, not a numbered block)**: the user pointed out
that pressing "Disconnect" to switch WebSDR sites also killed the
rigctld connection, which made no sense ("we'd only be interested in
switching sdrs, not trancievers"). This was a real architecture gap, not
just a button-label fix — `SyncEngine` was rewritten so the rig and
WebSDR subsystems have fully independent start/stop lifecycles
(`start_rig_from_other_thread()`/`stop_rig_from_other_thread()`/
`start_websdr_from_other_thread()`/`stop_websdr_from_other_thread()`),
with a single `SyncEngine` instance now created once at app startup and
living for the whole session (not recreated per Connect click). The GUI
was split into two independent panels (**WebSDR** and **Transceiver**),
each with its own Connect/Test buttons and status/error display; the
WebSDR panel's Connect button is context-aware — labeled **Connect** when
nothing's loaded, **Switch WebSDR** when a different site is selected
while one's already active (same call either way, the engine just
replaces whatever's loaded), **Disconnect** when the selected site is the
one that's active. `preflight.PreflightResult` was split into
`RigPreflightResult`/`WebsdrPreflightResult` to match. Covered by
`tests/test_engine_switch_site.py` (renamed in spirit, not just for
site-switching anymore — proves rig/WebSDR start/stop never touch each
other). All 44 tests passed after this round; live-tested via the running
app (connect rig once, switch WebSDR sites repeatedly, confirm
Transceiver panel stays connected throughout).

v4 added 3 more blocks (R–T) per a follow-up request to research and
support a third WebSDR software family, OpenWebRX (likely the most
common self-hosted WebSDR by receiver count):

| Block | What | File(s) |
|---|---|---|
| R. OpenWebRX driver | New driver for the OpenWebRX/OpenWebRX+ family. Unlike the other two drivers, control is via a jQuery-widget object (`$('#openwebrx-panel-receiver').demodulatorPanel().getDemodulator()`), frequency is offset-relative to a `center_freq` global (not absolute), and mode strings have no narrow/wide suffix convention (confirmed live via `Modes.getModes()`) | `websdr/openwebrx.py` |
| S. Registration | `DRIVERS["openwebrx"]`; added an OpenWebRX example site to `KNOWN_SITES` and swapped the flaky KiwiSDR example for the healthy one from v3.5 | `websdr/registry.py`, `config.py` |
| T. Tests + docs | `test_openwebrx_mode_mapping.py`; extended `test_fingerprint.py` with OpenWebRX + three-way-ambiguous cases; README + this file updated | `tests/`, `README.md`, `project_brief.md` |

Prior single-file prototypes (`websdrSync.py`, `websdrSync1.1.py`,
`websdrSync.1.2.py`) are still in the repo root — superseded by the
`sdrsync/` package, safe to delete once the package is confirmed working
against real hardware, or keep as reference.

### Review passes done so far (plan review + implementation review each round)
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
6. Opus reviewed the **v4 plan** before coding, catching 12 issues fixed
   before implementation, several substantive: (a) the readiness gate
   drafted for `attach()` checked a different JS access path
   (`getDemodulators()`) than the control calls actually use
   (`$('#openwebrx-panel-receiver').demodulatorPanel().getDemodulator()`)
   — the exact "gate passes, control call still throws" bug class already
   hit twice before; (b) the range guard could fail *open* if
   `window.bandwidth` weren't included in the readiness check (`NaN > x`
   is `false` in JS); (c) the plan omitted `cw_offset_hz` from the driver
   constructor entirely — since `engine.py` constructs every driver
   identically and the call site isn't wrapped in try/except, a
   constructor mismatch would raise an exception that's silently
   swallowed (scheduled via `run_coroutine_threadsafe` with nobody
   awaiting the future), making Connect appear to do nothing with zero
   error anywhere; (d) same-`page.evaluate` write-then-readback was being
   treated as delivery confirmation, but it can only prove the page's own
   client-side guard accepted the write, never that a dead control
   WebSocket didn't swallow it — needs the same explicit `readyState`
   check `kiwisdr.py` already has; (e) **unique to this driver's
   offset-relative frequency model**: if the SDR profile's `center_freq`
   changes after a successful tune (someone switches profile in the
   browser, or the server pushes a new center on reconnect), the
   demodulator keeps its stale offset and the receiver silently drifts to
   the wrong absolute frequency, since the rig's own frequency hasn't
   moved so nothing would re-trigger a push — no existing driver has this
   failure mode since the other two push absolute frequencies; (f)
   treating "frequency outside the active profile's range" as
   equivalent-cost to an unmapped hamlib mode understated it — an
   unmapped mode is rare/transient, while a rig parked outside range is
   the expected steady state for the exact scenario this limitation
   exists for, and retrying every 200ms tick against the unrotated,
   uncapped `sdrsync.log` is a real disk-growth risk; (g) three
   conventions already established (and each already bug-fixed once) in
   the other two drivers were missing from the plan text: the `attached`
   property itself, clearing all `_last_*_error` fields on a successful
   `attach()`, and `get_status()` setting `_attached = False` on a caught
   `PlaywrightError`; (h) `get_status()`'s frequency arithmetic risked
   repeating the exact NaN-crash bug already found and fixed once for
   KiwiSDR. All fixed in the plan before any code was written — see
   `C:\Users\ABEL75\.claude\plans\rippling-roaming-forest.md`'s v4
   section for the full revised Block R text.
7. Opus reviewed the **v4 implementation** and found 4 real bugs, all
   fixed and then verified against the live OpenWebRX example instance:
   - **Drift-recovery reload was undoing the exact recovery it was meant
     to do.** Detaching on a detected `center_freq`/`bandwidth` change
     routed through the normal attach path, which unconditionally
     `page.goto()`'d — a full reload that resets OpenWebRX back to its
     *default* SDR profile. An operator who'd manually switched to a
     non-default profile in the browser tab would have that choice
     silently reverted every time drift-detection fired. Fixed by having
     `attach()` check the readiness predicate **in place first** (a
     harmless no-op `page.evaluate` on a fresh/blank page) and only
     navigate if it's not already satisfied — so a drift-triggered
     reattach re-baselines against whatever profile is actually loaded
     instead of discarding it.
   - **`typeof x === 'number'` doesn't exclude `NaN`** in both the
     readiness predicate and `tune_hz`'s range check — `Math.abs(NaN) >
     n` is `false`, so a transiently-`NaN` `center_freq` could pass the
     in-range check and get a `NaN` offset written to the live
     demodulator (Python-side `isfinite` checks caught the aftermath and
     kept the engine from latching it, but the page had already been
     handed a bad value). Fixed by switching to `Number.isFinite()`.
   - **A dropped control WebSocket went undetected indefinitely once the
     rig stopped moving.** `get_status()` read `ws.readyState` but never
     acted on it; the actual recovery trigger (`tune_hz`/`set_mode`'s own
     `readyState` re-checks) only runs when the engine has a reason to
     call them, which stops happening once a frequency/mode is latched as
     already-sent. Fixed by having `get_status()` itself detach when
     `ws_ready` is false, the same treatment as the drift-detection path.
   - **The page-side `out_of_range` branch wasn't rate-limited**, only the
     local Python-side pre-check was — reachable whenever the cache was
     momentarily empty, defeating part of the point of the rate-limiting
     fix from the plan review. Fixed by mirroring the same
     compare-before-logging guard in both places.
   Verified afterward with a standalone script driving the real
   `OpenWebRXDriver` against `sdr2.justjakob.de`: clean attach, an
   in-range `tune_hz` that verifies to the exact requested Hz, an
   out-of-range `tune_hz` correctly rejected with the repeat call
   downgraded to `debug` (not re-logged as `warning`), `set_mode('USB')`
   succeeding, and `set_muted(True)`/`set_muted(False)` completing
   without error. One unrelated, harmless observation from that run: the
   site's own JS occasionally logs a console error
   (`TypeError: Cannot read properties of null (reading
   'get_offset_frequency')` inside `zoom_set`) that doesn't affect
   correctness and isn't triggered by anything this driver calls directly
   — a pre-existing quirk in that instance's waterfall/zoom code, not
   flagged as an action item.

### What's been verified so far
- `pytest` — 51/51 passing (pure-logic + stubbed-engine tests: mode
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
     **Done in v4**: swapped to `kiwisdr.areg.org.au:8073`.

5. **OpenWebRX driver (v4) — driver-level live testing done, app-level not
   yet, and its multi-profile limitation is a real, common-case gap, not
   an edge case.** `OpenWebRXDriver` was built, its individual JS calls
   confirmed live in a research browser tab, and — after the
   implementation review's 4 bug fixes — the whole driver (`attach`,
   in-range `tune_hz` with exact readback verification, out-of-range
   rejection + rate-limiting, `set_mode`, `set_muted`) was exercised
   end-to-end via a standalone script against a real receiver
   (`sdr2.justjakob.de`) and confirmed working. What's still missing is
   the same pass the other two drivers got: **through the actual app**
   (mock rig + real GUI session, Connect/Switch WebSDR buttons) rather
   than a standalone script — needs that before being considered as
   trustworthy as the other two in normal use.

   The bigger open item: some OpenWebRX stations run multiple SDR
   profiles (e.g. a separate HF device and a separate VHF/UHF device),
   switchable client-side via
   `ws.send(JSON.stringify({type: "selectprofile", params: {profile: id}}))`
   (source-confirmed, never live-tested — no multi-profile instance was
   available during research). `tune_hz()` reports a frequency outside the
   *currently active* profile's range the same way an unsupported hamlib
   mode is reported (logged once, shown in the WebSDR panel, retried
   without spamming — see the rate-limiting note in `openwebrx.py`), but
   does **not** attempt to automatically find and switch to a profile that
   would cover the requested frequency. For an operator who works HF and
   VHF/UHF on the same rig against a station like that, this means
   sdrsync will simply stop following them across that boundary until
   they manually pick the right profile in the browser tab. Needs a real
   multi-profile instance to research the missing piece: where the client
   learns each profile's frequency range *before* switching to it (the
   profile list populates a dropdown with just id/name pairs; the actual
   center/bandwidth seemed to only arrive *after* switching, in this
   round's research).

6. **"Save to list" button reported staying disabled even when the
   WebSDR panel's status shows "connected" (user report, 2026-08-06,
   not yet reproduced/root-caused).** Feature (v5): a "Save to list"
   button next to the Custom URL Detect result persists a proven-working
   Custom URL into `AppSettings.user_sites` (`config.py`) and adds it to
   the site dropdown; a "Delete" button next to the dropdown removes a
   previously-saved entry (confirmation dialog first). Both wired up in
   `gui/app.py` (`_on_save_site_clicked`, `_on_delete_site_clicked`,
   `_update_websdr_controls()`'s `can_save`/`can_delete` gating,
   `_resolve_selected_site`/`_find_any_site_by_url`/`_site_already_saved`
   helpers). `can_save` requires all of: Custom URL currently selected,
   WebSDR subsystem active, the *exact* resolved site URL matching
   `self._active_websdr_site.url`, `websdr_conn_var == "connected"`, and
   the site not already present in `KNOWN_SITES`/`self._user_sites`. User
   reported the button stayed disabled despite the status label reading
   "connected" -- by explicit instruction, not investigated or fixed yet
   ("leave it at that"). Suspect areas for a future pass: a URL-string
   mismatch between what `_resolve_selected_site()` returns and
   `self._active_websdr_site.url` (e.g. trailing slash, or a stale
   `_custom_site` vs. what's actually loaded), or `_update_websdr_controls()`
   not being re-invoked by the specific snapshot that flips status to
   "connected" in this scenario. Needs a live click-through with the
   Custom URL flow to reproduce before touching the code.

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
2. `pytest` to confirm the 51 tests still pass after the restart.
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
