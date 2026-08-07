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

## Roadmap

1. **Replace Playwright/Chromium with an embedded browser (v5) -- DONE,
   merged to master.** Plan at
   `C:\Users\ABEL75\.claude\plans\rippling-roaming-forest.md` (full
   history: why pywebview was tried and rejected -- it unconditionally
   requires the process main thread, conflicting with the GUI already
   owning it -- the pivot to embedding `wx.html2.WebView` as a widget
   inside a full wxPython rewrite instead, the Block A/B/C spikes, and
   two independent review passes that each found and fixed real
   load-bearing bugs before/after implementation). Final shape: Tkinter
   replaced entirely by wxPython; `sdrsync/browser/backend.py` (WebView2
   backend startup hardening), `sdrsync/websdr/browser_shim.py`
   (`WxPageAdapter`, the Playwright-`Page`-API-compatible shim the three
   WebSDR drivers use with only an import-line change),
   `sdrsync/gui/webview_host.py` (the persistent offscreen host frame +
   per-connection WebView child widget, mirroring Playwright's
   Browser/Page lifetime split), and a full `sdrsync/gui/app.py` rewrite.
   Verified via pytest (53 tests), a live engine-only script (mock rig +
   real websdr.org attach + real frequency sync + clean shutdown), and
   live multi-cycle manual testing through the actual app (multiple
   WebSDR connect/switch/disconnect cycles across two KiwiSDR sites, rig
   reconnects, ~8 minutes, clean exit). One real bug found via that
   manual testing (a normal WebView close() was also firing the
   "recreate me" signal meant only for unexpected death, racing the
   engine's event loop closing on app shutdown) -- fixed. Linux/macOS
   backends (WebKitGTK, Cocoa/WebKit) were NOT touched or verified --
   this round only covered the Windows/WebView2 backend, since that's
   the only OS available in this dev environment; treat cross-platform
   as still open, not assumed working.
2. **v6 hardening round -- DONE, 2026-08-07.** After the v5
   browser migration, asked for a 1.0-readiness brainstorm (own read +
   an independent Opus pass, both grounded in the actual code, not
   generic checklist advice). User picked the top 5 findings to
   implement now, ordered ahead of flrig; explicitly out of scope for
   this round: bidirectional sync (one-way transceiver->WebSDR is a
   fixed design decision) and Linux/macOS support (already a tracked,
   separate gap). Following the usual plan review -> implement -> review
   -> verify cycle. Live progress/resume-state tracked via TaskList
   (tasks #29-#35) and updated here after each block completes, in case
   of a session break. The 5 items, in the order they'll be implemented:
   1. **Fix the dead "Hide browser window" setting.** Verified bug:
      `AppSettings.headless` is written by `gui/app.py` but nothing in
      `gui/webview_host.py` reads it post-wxPython-migration -- the
      WebView is now *always* off-screen regardless of the checkbox, so
      there's currently no way to ever see a WebSDR's waterfall/spectrum
      through the app. A real regression from the v5 rewrite, not a
      pre-existing gap.
   2. **Fix `RigctldClient.get_mode()`'s RPRT hang.** Verified bug:
      `get_mode()` (`rig/rigctld.py`) unconditionally reads two lines for
      the `m` command; a rig/backend replying with a single-line
      `RPRT -x` error leaves the second `readline()` blocked until
      `cmd_timeout`, forcing a disconnect/reconnect instead of degrading
      to frequency-only sync.
   3. **Config robustness.** `AppSettings.load()`'s `user_sites` entries
      are used unguarded downstream (`gui/app.py`), so a hand-edited or
      partially-written `config.json` crashes at startup with a bare
      traceback; `save()` is also a non-atomic `write_text` (a crash
      mid-write can truncate the config). Validate/skip malformed
      entries; make `save()` write-temp-then-replace.
   4. **License + packaging basics.** No `LICENSE`, no `pyproject.toml`/
      version string anywhere, and the superseded `websdrSync*.py`
      prototypes are still sitting in the repo root. User chose
      **GPL-3.0** for the license (asked explicitly, since it's a
      decision only they can make). Add `LICENSE`, a `pyproject.toml`
      entry point, `__version__` logged at startup, remove the old
      prototype files.
   5. **Configurable poll interval.** `SyncEngine`'s poll loop is a fixed
      `POLL_INTERVAL_S = 0.2` issuing 3 CAT commands/tick (15/sec) with
      no way to slow it down -- risky on slower rigs (e.g. 4800-baud
      CI-V) or when rigctld is shared with other software. Make it a
      setting with a GUI control.

   **Plan review (Opus, independent pass, done 2026-08-07) — 2 corrections
   made before implementation, rest confirmed sound:**
   - **Item 1 (headless fix) had a real blocking conflict, caught and
     verified against the actual code.** `WebViewHost.present()`
     (`gui/webview_host.py`) hardcodes `ON_SCREEN_POS`/`OFF_SCREEN_POS`;
     `WxPageAdapter._simulate_click`'s audio-unlock click (`browser_shim.py`
     line ~500) always calls `on_screen_presenter(False)` in its `finally`
     — so naively "show the frame on connect when not headless" would get
     silently yanked back off-screen the moment audio unlocks, on every
     connection. **Revised fix**: `WebViewHost` needs its own "rest
     position" concept (on-screen when not headless, off-screen when
     headless) that `present(False)` restores to, rather than a hardcoded
     off-screen constant. Live-toggle-while-connected is still out of
     scope for this round, confirmed.
   - **Item 4 (packaging) had a real blocker**: `tests/__init__.py` exists,
     so setuptools' auto-discovery would see two top-level packages
     (`sdrsync` and `tests`) and error on `pip install -e .` without an
     explicit `[tool.setuptools.packages.find] include = ["sdrsync*"]`.
     Also: `main()` confirmed to exist at `gui/app.py`, so
     `sdrsync.gui.app:main` is the correct entry point; use
     `dynamic = ["version"]` sourced from `sdrsync/__init__.py` to avoid
     version drift; keep `pytest` out of the packaged install (optional
     extra, not a hard dependency); `rig/rigctld.py`'s docstring citing
     `websdrSync.1.2.py` needs updating alongside the file deletions.
   - **Item 2 (rigctld RPRT) confirmed correct, motivation sharpened**: the
     failure mode isn't just "hangs" — the blocked `readline()` hits the
     1s `cmd_timeout`, the client then calls `close()`, so it's a full
     disconnect/reconnect every affected tick; worse, if a stray reply
     *does* eventually arrive, the leftover `readline()` desyncs the next
     command's response from its request. Fix belongs in a small pure
     helper next to `parse_mode_response()` so the existing rigctld
     parsing test file can cover it directly; `fake_rigctld.py` already
     emits `RPRT -11` for unknown commands, a ready-made test fixture.
   - **Item 3 (config robustness) confirmed correct, scope widened
     slightly**: `user_sites` is the only list-of-dicts field, but
     `AppSettings.load()` does `cls(**filtered)` with no type-checking on
     *any* field, so a hand-edited scalar (e.g. `rigctld_port: "4532"`)
     also passes through uncaught — worth coercing/validating scalars too,
     especially since items 1 and 5 below add new fields. Also: since
     `_persist_user_sites` rewrites the whole list from in-memory objects,
     an entry skipped at load time is silently dropped on the next save —
     log skips at WARNING, not DEBUG, so this is visible. Atomic save
     should `fsync()` before `os.replace()`, not just write-then-replace.
   - **Item 5 (poll interval) confirmed correct, one implementation
     refinement**: read `self.settings.poll_interval_s` live inside the
     poll loop rather than threading it through `__init__` — the engine
     already reads `self.settings` live elsewhere, so this is simpler and
     gives free live-updates without a reconnect. Note `FREQ_DEBOUNCE_S`
     (0.2s) interacts with this: any interval ≥0.2s makes the debounce
     window satisfiable in a single tick, degrading jitter filtering to
     just the threshold check — not a bug, but worth a GUI-control clamp
     (~0.05–5.0s) so a user can't pick a value that defeats debouncing
     entirely by accident.

   **Revised implementation order** (per the review): rigctld RPRT fix
   first (fully isolated), then config robustness (since items 1 and 5
   both add fields it should validate), then the headless fix, then poll
   interval, then license/packaging last.

   **Progress (resume point if this session ends -- update after each
   block, per explicit user instruction):**
   - [x] **Item 2, rigctld RPRT fix — DONE.** Added `is_rprt_error()`
     helper (`rig/rigctld.py`) checking a response line for the `RPRT`
     error prefix; `get_mode()` now checks the first readline result
     before attempting the second, returning `None` immediately instead
     of blocking until `cmd_timeout` and disconnecting. `fake_rigctld.py`
     gained a `FakeRigState.mode_error` flag (emits `RPRT -11` for `m`
     when set) as a test fixture. New tests: `is_rprt_error` unit tests in
     `tests/test_rigctld_parsing.py`; a new `tests/test_rigctld_client.py`
     with a real-socket regression test proving the RPRT case returns
     `None` promptly, doesn't disconnect, and doesn't desync the next
     command's response (no leftover unread line). Full suite: 57/57
     passing (was 53; +4 new tests, this project has no pytest-asyncio
     dependency so the new async tests use the existing
     `asyncio.run(run())`-inside-a-sync-test pattern already used
     elsewhere in the suite).
   - [x] **Item 3, config robustness — DONE.** `config.py`:
     `_validate_scalars()` drops any scalar `AppSettings` field whose JSON
     value has the wrong type (e.g. a quoted `rigctld_port`), falling back
     to the dataclass default instead of silently storing a
     wrong-typed value or crashing; `_validate_user_sites()`/
     `_validate_user_site()` drop malformed `user_sites` entries (missing
     keys, wrong types, non-dict, non-list container) the same way,
     logged at WARNING (matches `gui/app.py`'s unguarded
     `d["name"]`/`d["url"]`/`d["driver_type"]` access, confirmed still
     exactly matches the validated shape). `save()` is now
     write-temp-then-`fsync()`-then-`os.replace()`, so a crash mid-write
     can't truncate the real config file. New `tests/test_config.py` (7
     tests): defaults-on-missing-file, malformed/non-dict/wrong-container
     `user_sites` entries all skipped correctly, wrong-type scalars fall
     back to defaults, garbage JSON doesn't crash `load()`, a full
     save-then-load round-trip, and no leftover `.tmp` file after a save.
     Full suite: 64/64 passing (was 57; +7).
   - [x] **Item 1, headless fix — DONE.** `gui/webview_host.py`:
     `WebViewHost` now takes a `headless` constructor arg and tracks a
     "rest position" (`_rest_pos()`) that's on-screen when not headless,
     off-screen when headless; `present(on_screen)` restores to
     `_rest_pos()` on `False` instead of the old hardcoded off-screen
     constant, and `set_headless()` lets the GUI update it. Fixed exactly
     the conflict the plan review caught: the audio-unlock click's
     `finally` block (`browser_shim.py`'s `_simulate_click`) always calls
     `on_screen_presenter(False)`, which would have silently re-hidden a
     visible window on every single connection if `present(False)` still
     hardcoded off-screen. `gui/app.py`: `WebViewHost` is now constructed
     with `headless=settings.headless` at startup, and
     `_webview_host.set_headless(self.settings.headless)` is called right
     before every Connect/Switch WebSDR click (picked up fresh per
     connection, not live-toggled while already connected -- confirmed
     out of scope for this round). Frame also given a real
     `VISIBLE_SIZE` (1000x700, was never sized before) and title changed
     from "(hidden WebSDR host)" to "SDRSync WebSDR" since it's not
     always hidden anymore. No unit-test coverage is possible here
     (needs a real wx.App + WebView2), so verified with a standalone
     script against the real API instead (scratchpad, not committed):
     confirmed `headless=True` rests off-screen, `headless=False` rests
     on-screen, and — the specific regression the review caught —
     `present(True)` then `present(False)` correctly returns to the
     on-screen rest position when not headless, not off-screen. All
     checks passed. `pytest` unaffected (64/64, no new tests needed here
     since it's pure wx-state logic already covered by the live check).
   - [x] **Item 5, poll interval — DONE.** `config.py`: new
     `AppSettings.poll_interval_s: float = 0.2` (matches the previous
     hardcoded value, so no behavior change for existing users), added to
     `_SCALAR_TYPES` as `(int, float)` (the validator's type map now
     supports a type-tuple, with a small `_type_name()` helper for the
     warning message). `sync/engine.py`: `_poll_loop()` reads
     `self.settings.poll_interval_s` live each iteration (per the plan
     review's suggestion) instead of a module constant, so a GUI change
     applies on the *next* wait cycle with no reconnect needed --
     confirmed via test that a change only takes effect once the
     *currently in-flight* wait completes, not mid-wait (expected,
     `asyncio.wait_for`'s timeout is fixed at call time; not a bug). Old
     `POLL_INTERVAL_S` constant renamed `DEFAULT_POLL_INTERVAL_S` for
     documentation purposes only (nothing reads it anymore). `gui/app.py`:
     new `wx.SpinCtrlDouble` control ("Poll interval (s):") in the
     Transceiver panel next to host/port, clamped to
     `MIN_POLL_INTERVAL_S`/`MAX_POLL_INTERVAL_S` (0.05-5.0, new module
     constants) per the review's note that `FREQ_DEBOUNCE_S` is also
     0.2s so an interval at or above that starts degrading jitter
     filtering; `_on_poll_interval_changed` writes straight to
     `self.settings`+`save()` with no connect-gating needed, since the
     engine already reads it live. New `tests/test_engine_poll_interval.py`
     (3 tests: faster interval ticks more in a fixed window, a live
     settings change is picked up without a reconnect, and the default
     matches the old hardcoded 0.2); `tests/test_config.py` gained a
     poll_interval_s int-or-float/invalid-string-falls-back-to-default
     test. Verified `wx.SpinCtrlDouble` against the real wx API + that
     `gui/app.py` still imports cleanly (standalone check, not committed).
     Full suite: 68/68 passing (was 64; +4).
   - [x] **Item 4, license/packaging — DONE.** Added `LICENSE` (GPL-3.0,
     fetched verbatim from a GitHub-hosted mirror of the canonical FSF
     text after a direct `gnu.org` fetch timed out, frontmatter stripped,
     675-line body confirmed matching the standard GPLv3 text). Added
     `sdrsync/__init__.py` with `__version__ = "1.0.0"`, logged once at
     startup in `gui/app.py`'s `SDRSyncApp.OnInit()`. Added
     `pyproject.toml`: `dynamic = ["version"]` sourced from
     `sdrsync.__version__` (no drift risk), `[project.scripts]` entry
     point `sdrsync = "sdrsync.gui.app:main"`, `pytest` moved to an
     `optional-dependencies.test` extra rather than a hard runtime
     dependency, and (the confirmed real blocker from the plan review)
     an explicit `[tool.setuptools.packages.find] include = ["sdrsync*"]`
     so `tests/`'s own `__init__.py` doesn't make setuptools' package
     auto-discovery see two top-level packages and fail. Deleted the
     three superseded `websdrSync*.py` prototypes from the repo root
     (`git rm`); updated `rig/rigctld.py`'s docstring, which cited one of
     them, to drop the reference. README: added an `pip install -e .`
     alternative to the Install section, a License section, and an
     explicit "Platform support: Windows only for now" note (Linux/macOS
     already tracked as a separate gap, not re-litigated here).
     **Verified for real, not just written**: `pip install -e .
     --no-deps` actually run in this environment -- confirmed it
     succeeds, confirmed `python -c "import sdrsync; print(sdrsync.
     __version__)"` and the `sdrsync` console-script entry point both
     resolve correctly via `importlib.metadata`, then uninstalled the
     editable install again to leave the dev environment as it was.
     `sdrsync.egg-info/` (generated by that install) is already covered
     by `.gitignore`'s existing `*.egg-info/` pattern -- confirmed via
     `git status --ignored`. Full suite: 68/68 passing, unaffected (no
     new tests needed -- this block is packaging metadata + a real
     `pip install -e .` run as verification, not app logic).

   **Implementation review (Opus, independent pass over the whole v6
   diff, done 2026-08-07) — 3 real bugs found, all fixed and re-verified
   against the real APIs:**
   - **`AppSettings.load()` crashed on non-object top-level JSON**
     (`config.json` containing `[]`, `"foo"`, `5`, or `null` — all valid
     JSON, just not an object). `data.items()` raised `AttributeError`,
     which the existing `except` clause didn't catch, so the app died at
     startup instead of falling back to defaults — inside the exact load
     path item 3 was meant to harden. Confirmed by reproducing it live,
     fixed with an explicit `isinstance(data, dict)` check before the
     `.items()` call, re-verified fixed live.
   - **The now-visible host frame (item 1's fix) had a close box and no
     `EVT_CLOSE` handler.** With `headless=False` (the default), an empty
     1000x700 "SDRSync WebSDR" frame now sits on-screen from startup;
     `FRAME_NO_TASKBAR` means no taskbar button, so clicking its X is the
     natural move — wx's default handler would `Destroy()` it, and every
     later `create_page()`/`present()`/`set_headless()` call would then
     touch a deleted C++ object. Unreachable before item 1 (the frame was
     always off-screen); genuinely new exposure from this round. Fixed:
     `WebViewHost.__init__` now binds `wx.EVT_CLOSE` to veto a
     user-initiated close; `gui/app.py`'s shutdown path already calls
     `frame.Destroy()` directly (not `Close()`), which doesn't fire
     `EVT_CLOSE`, so shutdown is unaffected — confirmed both halves live
     (user-close-attempt survives, explicit `Destroy()` still works).
   - **`poll_interval_s` was type-validated but not range-validated.** A
     hand-edited `0` or negative value passed `_validate_scalars`
     unchanged and reached `asyncio.wait_for(timeout=...)` in
     `_poll_loop`, turning it into a full-CPU busy loop hammering
     rigctld; the GUI's `wx.SpinCtrlDouble` only clamped its *displayed*
     value, never writing the clamped number back to `self.settings`.
     Fixed: `MIN_POLL_INTERVAL_S`/`MAX_POLL_INTERVAL_S` moved from
     `gui/app.py` into `config.py` (the canonical/authoritative location
     now, `gui/app.py` imports them) and a new `_clamp_poll_interval()`
     step in `load()` clamps an out-of-range value at load time, not just
     in the widget — confirmed live (`-5` and `9999` both correctly
     clamp to the bounds).
   Also confirmed clean (no action needed): `_SCALAR_TYPES`' bool-vs-
   `(int, float)` handling doesn't collide across fields; exactly one
   config load path exists, nothing bypasses the new validators;
   `is_rprt_error()` handles leading whitespace and can't false-positive
   on a real mode name; the atomic `save()`'s Windows behavior is
   correct; `_simulate_click`'s pre-existing `SafeYield` re-entrancy
   guard has no new interaction with the changed `present(False)`;
   `python -m sdrsync.main` and the `pyproject.toml`/version/entry-point
   wiring all still work correctly. Test quality was checked too, not
   just presence: the reviewer confirmed the new tests actually assert
   on the fixed behavior (e.g. the RPRT test proves no leftover unread
   line via a follow-up `get_freq()` call) rather than just "doesn't
   crash," and found no tautological tests.

   New regression tests added for the 3 fixes: `test_config.py` gained
   `test_load_clamps_out_of_range_poll_interval` and
   `test_load_does_not_crash_on_non_object_top_level_json`. The frame
   close-veto fix has no unit test (would need a real wx.App/WebView2,
   consistent with this project's existing practice for wx-level GUI
   logic) — verified instead via a standalone live script against the
   real wx API (not committed), same as item 1's original verification.

   **v6 hardening round is now complete.** Full suite: 70/70 passing
   (was 68; +2). All 5 items implemented, reviewed (plan review + this
   implementation review), fixed, and verified. Next up per the roadmap:
   flrig support (item 3 below).

   Other brainstormed items were deliberately left out of this round
   (not forgotten -- candidates for later): live-togglable window
   visibility while connected, `cw_offset_hz`/`mute_on_tx` GUI controls,
   an "open log folder" button, log rotation (already known), the
   unreproduced "Save to list stays disabled" bug and the
   URL-string/display-text comparison fragility suspected to underlie
   it, renamable/editable saved sites, and first-run onboarding.
3. **v7: flrig support -- PLANNED, implementation starting.** Full plan
   at `C:\Users\ABEL75\.claude\plans\rippling-roaming-forest.md`'s "v7 --
   flrig support" section (approved 2026-08-07). Researched against
   flrig's actual C++ source (`github.com/w1hkj/flrig`,
   `src/server/xml_server.cxx`), not just its HTML docs, which turned out
   to disagree with the source on frequency formatting. Key design:
   `RigState` moves to a new `rig/base.py` (was in `rigctld.py`); new
   `rig/flrig.py` (`FlrigClient` wrapping `xmlrpc.client.ServerProxy` via
   `run_in_executor`, a `TimeoutTransport` subclass since stock
   `ServerProxy` has no `timeout=`); new `rig/fake_flrig.py` (a threaded
   `SimpleXMLRPCServer` mock, since XML-RPC has no native asyncio
   integration -- non-blocking `close()`/`wait_closed()` designed
   explicitly to avoid the class of shutdown bug the v5 round hit once
   already); a new `RigClient` Protocol in `sync/engine.py` (mirroring
   the existing `WebViewHost` Protocol) so `self._rig` isn't concretely
   typed to `RigctldClient` anymore; `AppSettings.rig_backend`/
   `flrig_host`/`flrig_port` fields; a GUI "Backend:" dropdown in the
   Transceiver panel (each backend keeps its own persisted host/port).
   User decisions made explicitly before planning: build the flrig mock
   server now (not deferred), dropdown-next-to-existing-fields for
   backend selection (not a separate panel). **Real flrig software is
   not available in this dev environment** -- the plan explicitly flags
   that `rig.get_bw`'s exact shape (which varies by rig model per the
   source) needs live verification against a real flrig instance before
   this backend is as trusted as rigctld, same caveat every WebSDR driver
   got before its own live testing.

   **Progress (resume point if this session ends -- update after each
   block, per established practice from the v6 round):**
   - [x] Research (flrig source) + plan drafted + user decisions
     confirmed + plan approved.
   - [x] **Independent plan review (Opus pass) -- DONE.** No architectural
     blocker; 3 real fixes required before coding, all confirmed live on
     this machine (Python 3.12.10) before accepting them:
     (1) `SimpleXMLRPCServer.allow_reuse_address` defaults `True` and
     silently hijacks an already-bound port on Windows (confirmed live --
     `asyncio.start_server` correctly raises on a taken port,
     `SimpleXMLRPCServer` does not) -- `fake_flrig.py` must set it
     `False` explicitly; (2) the RPC-call exception tuple was missing
     `http.client.HTTPException` (confirmed via MRO: `BadStatusLine`
     subclasses neither `OSError` nor `xmlrpc.client.Error`) -- reachable
     if a user points the flrig backend at a real rigctld port by
     mistake, and would otherwise freeze the GUI's status updates
     silently; (3) the mock server's handle needs to expose the actual
     bound port (no `asyncio.Server`-style `.sockets[0].getsockname()`
     analogue exists for `SimpleXMLRPCServer`) so `port=0`-based tests
     can connect. Also confirmed: `TimeoutTransport` is load-bearing, not
     optional -- reproduced a 120s+ hang in `run_in_executor` without it
     (`asyncio.run()`'s executor shutdown has no timeout in 3.12). Full
     findings recorded in the plan file's "Plan review" section.
   - [x] **`rig/base.py` + `rig/flrig.py` (`FlrigClient` + parsers) +
     `rig/fake_flrig.py` mock server -- DONE, both blocks together
     (client tests needed the mock server, so implemented in one pass).**
     `RigState` moved to new `rig/base.py`; `rigctld.py` re-exports it
     unchanged (`from sdrsync.rig.rigctld import RigState` still works,
     confirmed via `pytest`). `rig/flrig.py`: `TimeoutTransport`
     (`xmlrpc.client.Transport` subclass, since stock `ServerProxy` has
     no `timeout=`), 4 pure parsers
     (`parse_freq_response`/`parse_mode_response`/
     `parse_bandwidth_response`/`parse_ptt_response`, each defensive per
     the source-confirmed wire shapes), `FlrigClient` mirroring
     `RigctldClient`'s public shape with every real RPC call dispatched
     via `run_in_executor` + `asyncio.wait_for`, catching
     `(OSError, xmlrpc.client.Error, http.client.HTTPException,
     ExpatError)` per the plan review's fix #2. `rig/fake_flrig.py`:
     `FakeFlrigState` (same attribute names as `FakeRigState`),
     `_FlrigXMLRPCServer` with `allow_reuse_address = False` explicitly
     set (plan review's fix #1 -- confirmed live this actually prevents
     the Windows port-hijack: a second bind on an already-listening port
     now correctly raises `OSError [WinError 10048]`, matching
     `asyncio.start_server`'s behavior), threaded `serve_forever`,
     non-blocking `close()`/`wait_closed()` split (fire `shutdown()` on a
     throwaway thread, join the real server thread via
     `run_in_executor`), `FlrigMockServerHandle.port` property (plan
     review's fix #3, needed since `SimpleXMLRPCServer` has no
     `asyncio.Server`-style socket list) -- confirmed live: a fast
     stop-then-restart on the same port works cleanly. New
     `tests/test_flrig_parsing.py` (18 tests) and
     `tests/test_flrig_client.py` (4 tests: normal `get_state()` round
     trip, dual-DSP-shape bandwidth handling, reconnect-after-mock-
     server-restart, and a direct timeout test proving
     `TimeoutTransport` actually bounds a connection that accepts TCP
     but never speaks HTTP -- takes real wall-clock time to run, by
     design, since it's proving a timeout fires). Full suite: 92/92
     passing (was 70; +22).
   - [x] **`sync/engine.py`: `RigClient` Protocol + backend wiring --
     DONE.** New `RigClient` Protocol (mirrors `WebViewHost`, same file)
     with exactly `ensure_connected()`/`get_state()`/`close()`;
     `self._rig: Optional[RigClient]` (was concretely `RigctldClient`);
     `self._mock_state` type widened to `Optional[FakeRigState |
     FakeFlrigState]`. `_start_rig`/`start_rig_from_other_thread` gained
     a `backend: str` parameter (`"rigctld"`/`"flrig"`), simple if/else
     branch (not a registry, per the plan's explicit anti-over-
     engineering call) picks the mock-server-start function and the real
     client class -- mock-mode principle preserved: `self._rig` is
     always the real client class even in mock mode, pointed at the
     mock server. `_stop_rig()`/`_tick()` needed zero changes (already
     backend-agnostic through the new Protocol). Fixed the existing
     `tests/test_engine_switch_site.py` call site the signature change
     breaks (confirmed by the plan review as a required fix, not
     optional). New `tests/test_engine_rig_backend.py` (3 tests):
     `_start_rig("rigctld"/"flrig", ...)` constructs the right client
     class, and switching backends mid-session replaces the client
     cleanly -- exercised directly with no mocking needed, since neither
     client connects anything at construction time. Full suite: 95/95
     passing (was 92; +3).
   - [x] **`config.py`: `flrig_host`/`flrig_port`/`rig_backend` fields +
     validation -- DONE.** New fields alongside the existing
     `rigctld_host`/`rigctld_port` (each backend keeps its own pair, per
     the user's explicit requirement); all three added to
     `_SCALAR_TYPES`; new `RIG_BACKENDS = {"rigctld", "flrig"}` constant
     (public, not underscore-prefixed, so `gui/app.py`'s dropdown can
     reuse it instead of a second hardcoded list) and
     `_validate_rig_backend()` (mirrors `_clamp_poll_interval`'s
     layering -- runs after `_validate_scalars`, only checks set
     membership). `tests/test_config.py` gained 4 tests: valid/invalid
     `rig_backend` (invalid falls back to `"rigctld"`), wrong-type
     `flrig_host`/`flrig_port` fall back to defaults, and a full
     save-then-load round trip of all three new fields. Full suite:
     99/99 passing (was 95; +4).
   - [x] **`preflight.py`: `check_flrig` -- DONE.** `main.get_version()`
     probe via `flrig.py`'s (now non-private) `TimeoutTransport`,
     standalone like `check_rigctld` (no `FlrigClient` instance needed).
     `RigPreflightResult`'s docstring updated to note it's shared by both
     backends now, not rigctld-only (the type itself needed no change --
     already just `ok`/`message`). Found and fixed one real bug of its
     own during verification: the outer `asyncio.wait_for` and the inner
     `TimeoutTransport` socket timeout used the same duration, so on an
     unreachable host they raced and `asyncio.TimeoutError` sometimes won
     with its empty message (`"...({})"` -- confirmed live, reproduced
     the empty-message case before fixing) instead of the inner, more
     informative `TimeoutError('timed out')`. Fixed by giving the outer
     wrapper a small buffer over the inner timeout, mirroring
     `FlrigClient.connect()`'s existing `connect_timeout > cmd_timeout`
     relationship, which avoids the same race there. Re-verified live
     (5 consecutive failure-path calls, all with the real message now).
     New `tests/test_preflight_flrig.py` (2 tests: success against
     `fake_flrig`, failure against a closed port). Full suite: 101/101
     passing (was 99; +2).
   - [x] **`gui/app.py`: Backend dropdown + wiring -- DONE.** New
     "Backend:" `wx.ComboBox(style=wx.CB_READONLY)` (`RIG_BACKEND_CHOICES`
     -- a fixed-order list, separate from `config.RIG_BACKENDS`'s
     unordered validation set) above the host/port row. Static box label
     "Transceiver (rigctld)" -> generic "Transceiver"; the "rigctld
     host:"/"Invalid rigctld port" strings genericized to "host:"/
     "Invalid port" (the plan review's minor finding). Host/port fields
     are now populated via `_populate_host_port_for_backend()` instead of
     hardcoded to rigctld's settings; `_save_current_backend_host_port()`
     + `_on_rig_backend_selected()` persist the currently-displayed
     values into whichever backend was active *before* reading
     `self.settings.rig_backend` and switching it -- this is what makes
     "switching back and forth never loses either backend's settings"
     actually hold. `_on_rig_connect_clicked` now writes host/port into
     the correct backend-specific fields (was unconditionally
     `rigctld_host`/`rigctld_port`), persists `rig_backend`, and calls
     `engine.start_rig_from_other_thread(backend, host, port, use_mock)`
     (4 args); disables `rig_backend_combo` alongside the existing
     `mock_rig_check`/`host_entry` disabling, re-enabled in
     `_apply_snapshot`'s disconnect branch. `_on_rig_test_clicked`/
     `_run_rig_test` branch on the selected backend to call `check_flrig`
     or `check_rigctld`. No committed wx-based test file (consistent
     with this project's established practice -- wx-level GUI logic
     needs a real `wx.App`, verified live instead, same as the v6
     headless-window fix). **Verified live** (standalone script against
     the real wx API, not committed): full `MainFrame` construction
     succeeds; switching rigctld -> flrig -> rigctld round-trips
     correctly; and the exact scenario the user required -- edit a
     field, switch away, switch back -- preserves both backends'
     independently-edited host/port values (explicitly asserted, not
     just eyeballed). Full suite: 101/101 passing throughout (no new
     tests needed here, per the above).
   - [x] **Implementation review (independent Opus pass, done
     2026-08-07) -- DONE, no blockers, 4 real findings fixed.** Full
     `git diff` review across all new/changed files, plus two live
     probes (not just reading code) to check whether the plan-review
     fixes actually hold:
     - **F1 (medium) -- the `TimeoutTransport` regression test was
       tautological, confirmed by reproducing it.** The original test
       asserted `connect() is False`, but that's true regardless of
       whether `TimeoutTransport` is used -- the *outer*
       `asyncio.wait_for` around `connect()` returns/raises on schedule
       either way; cancelling a `run_in_executor` future doesn't stop
       the underlying thread. Confirmed live: swapping in a stock
       `xmlrpc.client.Transport` still made the test's own assertion
       pass, while the *actual* protected behavior --
       `asyncio.run()`'s own executor-shutdown-on-exit -- hung past 60s
       with the same swap (reproduced directly). Fixed: the test now
       measures the wall-clock time of the whole `asyncio.run()` call
       (not just `connect()`'s return value) and deliberately does NOT
       tear down the stalling listener/thread until after `asyncio.run()`
       returns, so a regression can't be masked by external cleanup
       unblocking the leaked thread. Re-verified both directions: passes
       in ~4s with the real `TimeoutTransport`, hangs (killed at a 20s
       cap) when swapped back to a stock transport -- now a genuine
       regression test, not a tautological one.
     - **F2 (low) -- `preflight._flrig_probe_sync` never closed its
       `ServerProxy`**, leaking a cached `HTTPConnection` per Test-button
       click. Fixed with `with xmlrpc.client.ServerProxy(...) as proxy:`
       (confirmed `ServerProxy` supports the context manager protocol).
     - **F3 (low) -- `fake_flrig.py`'s standalone REPL accepted any `t`
       value unvalidated**, but unlike `fake_rigctld.py`'s equivalent
       (safe, since rigctld's wire format just echoes PTT as text),
       `rig.get_ptt`'s handler does `int(state.ptt)` -- a non-numeric
       value would raise inside the RPC handler on every subsequent
       poll. Fixed: REPL now only accepts `t 0`/`t 1`, with a comment
       explaining why this one (unlike rigctld's REPL) needs the check.
     - **F4 (nit) -- `fake_flrig.py`'s module docstring contradicted
       itself**, claiming no multi-field invariant needs cross-field
       atomicity while `push_mock_mode` does write `mode`+`passband_hz`
       together. Corrected: the actual reason this is still safe is that
       `rig.get_mode`/`rig.get_bw` are separate RPCs (mirroring real
       flrig, which has no combined call either), so a torn read here is
       the same kind of momentarily-stale pairing a client would see
       against a real instance, not a new failure mode this mock
       introduces. The GIL-atomicity reliance itself was confirmed
       accurate -- no path was found where a lock is actually needed.
     Also explicitly confirmed clean (checked in code, not just
     description): the exception tuple fix from the plan review
     (`http.client.HTTPException`) is present at both real RPC-call
     sites in `flrig.py` plus `check_flrig`'s own tuple; `allow_reuse_address
     = False` is real and effective; the mock-server `OSError` path is
     symmetric between backends; `_stop_rig()` needed no rigctld-specific
     assumptions; no stale 3-arg `start_rig_from_other_thread` signatures
     remain anywhere; GUI host/port persistence correctly saves the *old*
     backend's fields before switching; parsers match the confirmed
     wire format including the PTT bool-vs-int guard (covered by a
     genuine, non-tautological regression test). Full suite: 101/101
     passing after all four fixes.

   **v7 flrig support is now feature-complete and reviewed twice** (plan
   review + implementation review, matching this project's standard
   pattern). **Not yet done, explicitly out of scope for this
   environment**: a live click-through against real flrig software (none
   available here) -- see the "Not yet live-verified" caveat at the top
   of this v7 section, particularly `rig.get_bw`'s exact shape against a
   real rig model. Manual verification so far has been mock-mode only
   (via `fake_flrig.py`) plus the standalone wx-widget scripts described
   in the GUI block above -- a full live app click-through (mock rig +
   GUI session, not just standalone scripts) is still worth doing before
   calling this "as trustworthy as rigctld," time permitting.

4. **v8: site-list import/curated-update, Save-to-list hardening,
   CW-offset/mute-on-TX GUI, log rotation -- PLANNED, implementation
   starting.** Full plan at
   `C:\Users\ABEL75\.claude\plans\rippling-roaming-forest.md`'s "v8 --
   site-list import/curated-update, Save-to-list hardening, CW
   offset/mute-on-TX GUI, log rotation" section (approved 2026-08-07).
   4 items, scoped via `AskUserQuestion` from a follow-up to the earlier
   1.0-readiness brainstorm plus two new asks. OpenWebRX's multi-profile
   gap was raised again and explicitly deferred (still no multi-profile
   instance to test against). Key decisions confirmed with the user:
   `imported_sites` (file+URL loads, shared bucket) and `curated_sites`
   (GitHub auto-update) are separate `AppSettings` list fields, both
   replace-all semantics; curated list lives at
   `sites/websdr_sites.json` on `master` in this repo (to be added as
   part of this work); site-management GUI is a separate "Manage
   sites..." dialog, not inline buttons; `cw_offset_hz`'s new GUI
   control is disabled while a WebSDR session is active (mirrors the
   existing `headless_check` pattern), since it's only read at driver-
   construction time.
   - Item 1: site list from file/URL + curated-GitHub update
     (`config.py` new fields + stricter external-data validation, new
     `sitesource.py`, new `gui/site_manager_dialog.py`).
   - Item 2: "Save to list" stays-disabled hardening -- re-traced the
     full code path and found no bug in the straightforward flow (the
     "not re-invoked" theory in the original report is contradicted by
     the code); found one real, narrow inconsistency (the whole-engine-
     crash branch doesn't update the `_websdr_conn_text` mirror). Being
     shipped explicitly as hardening against a *suspected* fragility
     class (a stringly-typed gate with 3 writers, one with a gap), not a
     confirmed fix for an unreproduced bug -- said plainly, not
     oversold.
   - Item 3: `cw_offset_hz`/`mute_on_tx` GUI controls (both fields
     already exist in `AppSettings`, pure GUI wiring).
   - Item 4: log rotation (`RotatingFileHandler`, 5MB x 3 backups) +
     "Open log folder" button (`os.startfile`, Windows-only, matches
     current app scope).

   **Progress (resume point if this session ends -- update after each
   block, per established practice from v6/v7):**
   - [x] Research + plan drafted + user decisions confirmed (4
     `AskUserQuestion` rounds total across scoping and design) + plan
     approved.
   - [x] **Independent plan review (Opus pass) -- DONE.** No true
     architectural blocker but 3 real bugs found in the *design*, all
     confirmed against the actual code before accepting them and fixed
     in the plan: (1) the originally-planned `config.py` -> `websdr.
     registry` import for stricter external-site validation would have
     dragged the entire wx/WebView2 driver stack into what's today a
     dependency-free leaf module (confirmed via the real import chain:
     `registry.py` -> driver modules -> `browser_shim.py` -> `import
     wx`) -- moved that stricter validation into `sitesource.py`
     instead, where it belongs (the actual trust boundary is at fetch
     time, not every `AppSettings.load()`); (2) the plan's "one combined
     site-list accessor used everywhere" would have reintroduced a new
     "Save to list stays disabled" cause in the very round meant to
     harden against that, plus a silent restart-persistence bug --
     confirmed both by reading the exact call sites
     (`_site_already_saved`, `_on_websdr_connect_clicked`'s
     `last_site_driver_type` persistence) -- fixed by splitting into two
     accessors, one scoped exactly as today for those two, a new wider
     one only for dropdown display/name-lookup; (3) item 2's own fix as
     originally planned would have moved the "Save to list" staleness
     gap rather than closed it, since the fatal-error branch in
     `_apply_snapshot` returns early without touching any WebSDR state
     at all (confirmed by reading the branch) -- fixed by setting the
     new `_websdr_connected` boolean in every branch, including that
     one. Also found and fixed: a `logging.basicConfig()` no-op
     footgun that would have made the planned rotation test flaky
     (confirmed live: `basicConfig` silently does nothing once the root
     logger already has handlers, which pytest's own logging plugin can
     pre-attach) -- fixed with `force=True`; a `mute_on_tx`/`headless_check`
     precedent citation mix-up (corrected to cite
     `_on_poll_interval_changed`, the actual immediate-write precedent);
     an accepted-not-fixed note about `mute_on_tx` only being checked on
     PTT edges; name/URL collision handling added to the site-list
     validator (none existed in the original plan); and a "Manage
     sites..." button placement fix (the site row has no free grid
     columns left). Full findings and fixes recorded in the plan file's
     "Plan review" annotations throughout the v8 section.
   - [x] **Item 4, log rotation + open log folder button -- DONE.**
     `logging_setup.py`: swapped the plain `FileHandler` for
     `logging.handlers.RotatingFileHandler` (`LOG_MAX_BYTES = 5_000_000`,
     `LOG_BACKUP_COUNT = 3`), with `force=True` on `basicConfig()` per the
     plan review's fix (confirmed live this is load-bearing, not
     speculative -- without it, a pre-attached root handler, e.g. from
     pytest's own logging plugin, made `basicConfig()` silently install
     zero handlers). `gui/app.py`: new "Open log folder" button in
     `_build_widgets` (below the mock-rig panel, above `panel.SetSizer`),
     `_on_open_log_folder_clicked()` wraps `os.startfile(LOG_FILE.parent)`
     in try/except `OSError` with a `wx.MessageBox` on failure; new
     `import os` and `from sdrsync.logging_setup import LOG_FILE` imports.
     New `tests/test_logging_setup.py` (2 tests: installs a
     `RotatingFileHandler` with the expected `maxBytes`/`backupCount`, and
     `force=True` makes repeated `setup_logging()` calls idempotent rather
     than accumulating/leaking handlers) -- both manually confirmed
     non-tautological by reproducing the no-`force=True` failure mode
     live before accepting the fix. Full suite: 103/103 passing (was 101;
     +2). Live-verified: full `MainFrame` construction succeeds and
     `_on_open_log_folder_clicked()` runs without raising against the real
     wx API (standalone script, not committed).
   - [x] **Item 3, `cw_offset_hz`/`mute_on_tx` GUI controls -- DONE.**
     `gui/app.py`'s `_build_websdr_panel`: new row (after the existing
     headless-check row) with a `wx.SpinCtrl` for `cw_offset_hz`
     (`min=-2000, max=2000`, a `_Tooltip` explaining "Disconnect and
     reconnect to apply a new CW offset") and a `wx.CheckBox` for
     `mute_on_tx` ("Mute WebSDR on TX", a `_Tooltip` documenting the
     accepted PTT-edge-only limitation). `cw_offset_ctrl` is
     enabled/disabled in lockstep with `headless_check` in
     `_update_websdr_controls` (enabled when `_websdr_active` is False,
     disabled while active -- it's only read once at driver construction,
     `sync/engine.py`'s `_start_websdr`), and its value is written into
     `self.settings.cw_offset_hz` at the same point `headless` is
     (`_on_websdr_connect_clicked`, right before `settings.save()`).
     `mute_on_tx_check` uses the immediate-write-and-save pattern (new
     `_on_mute_on_tx_changed` handler, mirroring `_on_poll_interval_changed`
     per the plan review's precedent correction), since the engine reads
     `settings.mute_on_tx` live every PTT edge -- no reconnect needed, no
     enable/disable gating. Both fields already existed in `AppSettings`
     with `_SCALAR_TYPES` entries, so no `config.py` changes were needed.
     No new automated test (pure GUI wiring, no headless-wx harness in
     this project, consistent with items 1/5 from v6 and the backend
     dropdown from v7) -- verified live via a standalone script against
     the real wx API (not committed): initial values populate correctly
     from settings, `cw_offset_ctrl` toggles enabled/disabled correctly
     across `_websdr_active` True/False, and `_on_mute_on_tx_changed`
     writes through to `self.settings.mute_on_tx` correctly. Full suite:
     103/103 passing throughout (no new tests needed).
   - [x] **Item 2, Save-to-list gating hardening -- DONE.** New
     `self._websdr_connected: bool` field (init `False` in `__init__`,
     next to `_websdr_conn_text`), set in every branch of
     `_apply_snapshot`: `False` in the fatal-error branch (which now also
     sets `_websdr_active = False`/`_active_websdr_site = None`, calls
     `_update_websdr_controls()`, then explicitly re-`Enable(False)`s both
     connect buttons afterward -- needed because `_update_websdr_controls`
     would otherwise re-enable `websdr_connect_btn` off a stale
     `_rig_active` flag that this branch doesn't touch); `False` in the
     not-active branch; `bool(ws and ws.connected)` set **unconditionally**
     in the active branch (moved above the `if ws is not None:` check per
     the plan review's fix -- `ws` can be `None` while `websdr_active` is
     `True`). `can_save`'s gate now checks `self._websdr_connected`
     instead of `self._websdr_conn_text == "connected"`; `_websdr_conn_text`
     stays as display-text-only. Shipped as hardening against a suspected
     fragility class, not a confirmed fix for the unreproduced original
     report -- said plainly, not oversold. No new automated test (no
     headless-wx harness in this project); verified live via a standalone
     script (not committed) simulating both an active+connected snapshot
     (`_websdr_connected` -> `True`) and a fatal-error snapshot with a
     stale `_rig_active = True` (`_websdr_connected` -> `False`,
     `websdr_connect_btn`/`rig_connect_btn`/`save_site_btn` all correctly
     stay disabled, confirming the button-re-enable edge case the review
     flagged is actually closed). Full suite: 103/103 passing (no new
     tests needed, pure GUI-gating logic).
   - [x] **Item 1a, `config.py` `curated_sites`/`imported_sites` + shared
     validation -- DONE.** New `AppSettings` fields `imported_sites: list`
     and `curated_sites: list` (both `field(default_factory=list)`, same
     dict shape as `user_sites`). Refactored `_validate_user_site`/
     `_validate_user_sites` into `_validate_site_entry`/
     `_validate_site_list(filtered, key)` (the latter takes a field-name
     key instead of being hardcoded to `user_sites`), called three times
     in `load()` -- once per bucket, all at the same lenient shape-only
     tier per the plan review's fix (no `registry.DRIVERS` import here;
     the stricter driver-type-registered + collision check happens once,
     at fetch time, in the not-yet-written `sitesource.py`). No
     `_SCALAR_TYPES` entries needed (both are lists). New tests in
     `tests/test_config.py` (6, mirroring the existing `user_sites`
     coverage): defaults to `[]`, malformed entries skipped for each
     bucket independently, wrong-type value falls back for both at once,
     and a save-then-load round trip of both buckets together. Full
     suite: 108/108 passing (was 103; +5 -- one test covers both buckets'
     wrong-type case in one assertion pair).
   - [x] **Item 1b, `sitesource.py` fetch/load + `sites/websdr_sites.json`
     starter file -- DONE.** New `sdrsync/sitesource.py`:
     `validate_site_list(raw, existing_sites)` (strict tier -- rejects
     unregistered `driver_type`s via `sdrsync.websdr.registry.DRIVERS`,
     and name/URL collisions against `existing_sites` or an earlier entry
     in the same batch, per the plan review's fix), `load_site_list_from_file`
     (sync, GUI-thread-safe -- small local file), `fetch_site_list` (async,
     `run_in_executor`-wrapped, matching `preflight.py`'s shape --
     dispatched via `asyncio.run(...)` on a background thread the same way
     `_on_detect_clicked` already does). Its own `_fetch_body_sync` helper,
     deliberately not reusing `preflight._http_get_body_sync`: catches
     `urllib.error.HTTPError` separately (distinguishes "offline" from a
     real HTTP status) and caps the response at `MAX_RESPONSE_BYTES`
     (1MB) since the URL is user-supplied. New `SiteListFetchResult`
     (`GuiMessage` subclass: `bucket`, `sites`, `message`) -- not yet
     wired into `MainFrame._dispatch` (that's Block G's job). New
     `sites/websdr_sites.json`: shipped as a documented-empty `[]`, not a
     mirror of `KNOWN_SITES`' 3 entries as originally sketched --
     discovered during implementation that mirroring them would make the
     new collision-rejection logic reject every entry on first fetch
     (they'd collide with `KNOWN_SITES` itself), which is self-defeating;
     empty-but-valid was the plan's explicitly allowed fallback option.
     New `tests/test_sitesource.py` (13 tests): valid entries accepted;
     unregistered `driver_type` rejected; malformed shapes rejected;
     name collision, URL collision, and same-batch collision each
     rejected; non-list top-level handled; file-load success/missing/
     malformed-JSON/no-valid-entries; fetch rejects non-JSON body and
     propagates a fetch failure message (both via a monkeypatched
     `run_in_executor`, no real network needed for these). Full suite:
     121/121 passing (was 108; +13). **Live-verified**: loading the
     actual (empty) `sites/websdr_sites.json` correctly reports "No valid
     sites found"; a real fetch against the not-yet-pushed
     `CURATED_LIST_URL` correctly returns a distinct `HTTP 404` message
     (proving the HTTPError-status-code distinction works over a real
     network call) -- **the full end-to-end curated-fetch check (a real
     200 + real parsed sites) still needs `sites/websdr_sites.json`
     actually pushed to `github.com/Ar0xA/sdrsync` on `master`**,
     deferred to whenever the user next approves a push, per this
     project's established pattern of not pushing without being asked.
   - [x] **Item 1c, `SiteManagerDialog` + GUI wiring -- DONE.** New
     `sdrsync/gui/site_manager_dialog.py` (`SiteManagerDialog(wx.Dialog)`,
     genuinely new pattern for this codebase): "Load from file..."
     (`wx.FileDialog`), "Load from URL..." (`wx.TextEntryDialog`),
     "Update from GitHub" (fixed `CURATED_LIST_URL`), a `wx.ListBox`
     showing both buckets' entries with a "Remove selected" button. Talks
     directly to `AppSettings` (reads/writes `settings.imported_sites`/
     `settings.curated_sites`, `save()`s immediately) rather than
     duplicating `MainFrame`'s in-memory bookkeeping -- runs its own
     background fetch thread + its own `queue.Queue`/`wx.Timer`
     (mirroring `MainFrame`'s status-queue pattern but fully
     self-contained, not routed through `MainFrame._dispatch`). Shown via
     `ShowModal()`; confirmed the underlying event loop (and thus
     `MainFrame`'s own status-queue timer) keeps running while a modal
     dialog is open, so this doesn't stall the engine's status updates.
     `gui/app.py`: new "Manage sites..." button on its own grid row
     (after the site-selection row, per the plan review's placement fix
     -- that row has no free columns); `_on_manage_sites_clicked` opens
     the dialog with `existing_sites = KNOWN_SITES + self._user_sites`
     (matching `_find_any_site_by_url`'s scope, not the wider set), then
     rebuilds `self._imported_sites`/`self._curated_sites` from settings
     and refreshes the dropdown once it closes. New
     `self._all_selectable_sites()` accessor (`KNOWN_SITES + user_sites +
     curated_sites + imported_sites`) used ONLY by `_refresh_site_dropdown_values`
     and `_resolve_selected_site`'s name-lookup branch, per the plan
     review's two-accessor fix -- `_find_any_site_by_url`/
     `_site_already_saved`/the `last_site_driver_type` persistence check
     in `_on_websdr_connect_clicked` are UNCHANGED, still scoped to
     `KNOWN_SITES + self._user_sites` only (a docstring on
     `_find_any_site_by_url` now explains why, so this doesn't get
     "fixed" back to the wider set later). `_refresh_site_dropdown_values`
     now falls back to `KNOWN_SITES[0].name` when the previous selection
     no longer exists after a replace-all (was previously left unset).
     `MainFrame.__init__` builds `self._imported_sites`/
     `self._curated_sites` from settings the same way `self._user_sites`
     already is. No new automated test (wx-dialog-level GUI logic, no
     headless-wx harness in this project, consistent with every other wx
     block) -- **live-verified** via standalone scripts (not committed):
     the dropdown correctly includes imported/curated site names;
     `_find_any_site_by_url` correctly excludes them (the exact
     regression the plan review flagged); a simulated replace-all via
     `_apply_bucket_result` correctly updates `settings.imported_sites`;
     selecting a site that a replace-all then removes correctly falls
     back to `KNOWN_SITES[0].name` in the dropdown (the other flagged
     regression); the dialog itself constructs successfully with real
     widgets and closes cleanly. Full suite: 121/121 passing throughout
     (no new tests needed for this block).
   - [x] **Implementation review (independent Opus pass) -- DONE, 2
     blockers + 2 real-but-minor findings, all fixed and re-verified.**
     Full diff review across all v8 files, tracing actual code paths
     rather than trusting descriptions:
     - **Blocker 1 -- re-running "Update from GitHub" (or re-loading the
       same file) always failed after the first success.**
       `SiteManagerDialog._current_all_sites()` included BOTH buckets'
       current entries as collision context, including the very bucket
       about to be replaced wholesale -- so a second fetch validated the
       incoming list against its own previous results and rejected every
       entry as a "collision." Confirmed by reproduction: second update
       of identical data returned `[]`. Fixed: `_current_all_sites()`
       now takes an `exclude_bucket` param, passed at both call sites, so
       a fetch into a bucket is validated against the OTHER bucket +
       `existing_sites` only, never itself (intra-batch collisions were
       already handled by `validate_site_list`'s own accumulation, so
       nothing is lost). Re-verified live: two consecutive curated
       updates with identical data both succeed identically now.
     - **Blocker 2 -- empty-list and validation-failure were conflated,
       so a deliberately-emptied curated list could never actually clear
       the bucket.** `fetch_site_list`/`load_site_list_from_file` treated
       `not sites` (covers both "raw was `[]`" and "every entry got
       rejected") as a hard failure, returning `None` either way. Fixed
       with a shared `_finalize()` helper distinguishing three outcomes:
       non-list top-level (real error), a genuinely empty `[]` (valid,
       returns `([], message)` so the caller can clear the bucket), and
       all-entries-rejected (real error, message now includes the reject
       count). `tests/test_sitesource.py`'s `test_load_site_list_from_file_no_valid_entries`
       (which asserted the old, now-wrong behavior for `[]`) was
       replaced with `test_load_site_list_from_file_empty_list_is_a_valid_empty_result`
       plus a new `test_load_site_list_from_file_all_entries_rejected` for
       the genuinely-invalid case, and a `test_load_site_list_from_file_non_list_top_level`.
       Also noted (not a code bug, a rollout gap): `sites/websdr_sites.json`
       is still untracked/unpushed, so "Update from GitHub" 404s until a
       push happens -- flagged again below.
     - **Real-but-minor 1 -- unchecking `mute_on_tx` mid-transmission left
       the WebSDR muted indefinitely, not just until the next PTT edge as
       documented.** `sync/engine.py`'s PTT-edge handler gated BOTH the
       mute AND unmute calls behind the *current* `mute_on_tx` value; on
       the falling edge after the user unchecked the box, the unmute call
       was skipped entirely, and no future edge could ever fire it again
       since the gate stayed off. Confirmed via reproduction that this is
       worse than the documented "not instant" limitation -- it's "stuck
       until reconnect." Fixed: the falling edge now always unmutes
       regardless of the current `mute_on_tx` value (only the rising
       edge's mute stays gated) -- unmuting when not actually muted is a
       harmless no-op. New `tests/test_engine_mute_on_tx.py` (2 tests,
       `_tick()`-driven against stub rig/driver objects, mirroring
       `test_engine_mode_independence.py`'s pattern): confirmed
       non-tautological by reverting the fix and re-running -- the
       toggle-mid-transmission test correctly failed (`[True]` instead of
       `[True, False]`) against the pre-fix code, then re-confirmed
       passing after restoring the fix.
     - **Real-but-minor 2 -- `cw_offset_hz` edits were lost if the user
       never clicked Connect.** The only write of `settings.cw_offset_hz`
       was inside `_on_websdr_connect_clicked`; an edit made while
       disconnected sat only in the widget, so any *unrelated*
       `settings.save()` elsewhere (e.g. changing poll interval) would
       serialize the whole (stale) dataclass, silently discarding the
       edit before Connect ever ran. Fixed: new `_on_cw_offset_changed`
       handler (bound to `EVT_SPINCTRL`) writes-and-saves immediately,
       mirroring `mute_on_tx`'s pattern -- this only fixes persistence,
       the documented "must reconnect to apply" behavior for the engine
       itself is unchanged (still only read once, at driver construction).
       Verified live: editing the spin ctrl without clicking Connect,
       then triggering an unrelated save, no longer loses the edit.
     - Also confirmed clean (checked directly, not just described): the
       `_websdr_connected` three-way split in `_apply_snapshot` is
       correct in all branches including the fatal-error branch's
       `_update_websdr_controls()`-then-explicit-`Enable(False)`
       ordering; every `_find_any_site_by_url` vs. `_all_selectable_sites()`
       call site is on its intended (narrow vs. wide) side, with both
       plan-review-flagged regressions (Save-to-list staleness, restart-
       persistence) genuinely avoided; `SiteManagerDialog`'s threading has
       no GUI-thread violations and no post-`Destroy()` timer fire;
       `logging.basicConfig(force=True)` has no log-loss scenario since
       `setup_logging()` is only ever called once; `config.py`'s
       `_validate_site_list` refactor is byte-for-byte equivalent to the
       old `user_sites`-only validator. A few nitpicks noted but not
       fixed (cosmetic/low-value): stale Test/Detect text isn't cleared
       by the new dropdown-fallback path; `sitesource.py`'s URL fetch
       accepts any `urllib` scheme including `file://` (self-inflicted
       only, no privilege boundary in a desktop app); a monkeypatch trap
       in `_on_open_log_folder_clicked` (binds `LOG_FILE` by value) that
       only matters if someone later writes a test against it.
     Full suite: 125/125 passing (was 121 after Block F; +2 new
     `test_engine_mute_on_tx.py` tests, +2 net new `test_sitesource.py`
     tests from the Blocker 2 fix's test rewrite -- confirmed by direct
     `pytest` run).
   - [x] **pytest + live verification + final docs update -- DONE (this
     entry).** Full suite 125/125 passing. Live-verified end to end via
     standalone scripts against the real wx API (not committed, per this
     project's established practice): `MainFrame` construction with
     imported/curated sites present in settings; the dropdown correctly
     includes their names; `_find_any_site_by_url` correctly stays scoped
     to `KNOWN_SITES + user_sites`; a simulated replace-all correctly
     falls back to `KNOWN_SITES[0].name` when the active selection is
     dropped; `SiteManagerDialog` constructs and closes cleanly; both
     implementation-review blockers verified fixed directly (two
     consecutive identical curated updates both succeed; an empty-list
     result correctly clears `curated_sites` rather than being treated as
     a failure); the mute-on-TX unmute fix verified both by a direct
     logic-level reproduction and by reverting-and-re-testing against the
     pre-fix code to confirm the new test actually catches the
     regression; `cw_offset_hz` now persists on edit without requiring
     Connect first.
     **Update, after the user approved a commit+push (2026-08-07,
     `github.com/Ar0xA/sdrsync` commit `8eab952`)**: the final deferred
     verification is now done too. A real `fetch_site_list(CURATED_LIST_URL, ...)`
     call against the live, pushed `sites/websdr_sites.json` returns a
     genuine HTTP 200 and correctly reports `([], "...has an empty site
     list")` -- confirming both the fetch mechanism end-to-end and the
     Blocker-2 empty-vs-error distinction against production GitHub, not
     just against local fixtures.
     **Further update (2026-08-07, commit `90b0866`)**: at the user's
     request, populated `sites/websdr_sites.json` with 20 real public
     sites (7 KiwiSDR, 7 websdr_org, 6 OpenWebRX, ~15 countries) sourced
     from receiverbook.de listings. Each candidate was checked against the
     app's own `preflight.check_websdr_url` (real reachability) +
     `detect_websdr_type` (real driver-type fingerprint match, not
     assumed from the directory listing or URL naming) before inclusion
     -- roughly half the ~50 candidates gathered turned out unreachable
     (typical for dynamic-DNS home stations) and were dropped, not
     included speculatively. The final 20-entry batch was also confirmed
     to round-trip cleanly through `sitesource.validate_site_list` against
     `KNOWN_SITES` with zero rejections before committing. **Post-push,
     the real "Update from GitHub" path was exercised end-to-end**: a
     fresh `fetch_site_list(CURATED_LIST_URL, ...)` call now returns a
     genuine HTTP 200 with all 20 sites correctly parsed -- this is the
     first time the curated-update feature has actually delivered real
     sites through the full network path, not just an empty-list
     round-trip. No other manual GUI click-through was
     possible in this environment (no
     display automation for a native desktop app) -- consistent with
     every prior wx-heavy round in this project, verification here is
     standalone-script-against-the-real-API plus full pytest, not an
     actual mouse-driven session.

   **v8 round is now feature-complete and reviewed** (plan review +
   implementation review, matching this project's standard two-review
   pattern). All 4 items shipped: log rotation + open-log-folder,
   `cw_offset_hz`/`mute_on_tx` GUI controls, Save-to-list gating
   hardening, and WebSDR site-list import/curated-update via a new
   "Manage sites..." dialog. Full suite: 125/125 passing (was 101 at the
   start of this round; +24 new tests across `test_logging_setup.py`,
   `test_config.py`, `test_sitesource.py`, and `test_engine_mute_on_tx.py`).
   **Committed and pushed** (2026-08-07, `github.com/Ar0xA/sdrsync` commit
   `8eab952`), with explicit user approval. The end-to-end "Update from
   GitHub" fetch was verified live against the pushed
   `sites/websdr_sites.json` afterward (real HTTP 200, correctly parsed
   as a valid empty list) -- see the implementation-review entry above
   for details.

## Linux port feasibility spike (WSL, 2026-08-07) -- promising, not a go/no-go decision yet

Per the user's question about macOS/Linux support, ran a Block-A-style
spike against a real (if not bare-metal) Linux environment: WSL2's
Debian 12 distro, using WSLg (Windows 11's built-in Wayland/XWayland +
PulseAudio bridge to the host) for display and audio. System packages
installed by the user (`sudo apt install python3-wxgtk4.0
python3-wxgtk-webview4.0 libwebkit2gtk-4.1-0 pulseaudio-utils`) --
Debian ships wxPython as a prebuilt system package (wx **4.2.0**, vs.
4.3.1 on Windows), no source build needed.

**Both of the original Windows spike's load-bearing go/no-go questions
were re-tested against a real live WebSDR site (`websdr.ewi.utwente.nl:8901`)
and both held on Linux too:**
- **Autoplay gate is real and needs the same two-step unlock** already
  implemented in `websdr_org.py`'s `_satisfy_audio_gate()` (a genuine
  `wx.UIActionSimulator` corner click, then an explicit
  `document.ct.resume()` if still `'suspended'`) -- confirmed `ct.state`
  stayed `'suspended'` after the click alone, only reached `'running'`
  after the resume() call, exactly mirroring the Windows finding.
- **A hidden/off-screen window does not kill audio.** Verified with
  *objective* evidence this time, not just the JS side's own
  self-reported state: `pactl list sink-inputs` showed a real
  PulseAudio stream (`media.role = "webaudio"`, `Corked: no`, owned by
  the `WebKitWebProcess`) appear once unlocked, and it **stayed
  `Corked: no`** (i.e. still actively playing) after moving the host
  frame to `(-32000, -32000)` via `SetPosition()` -- the exact off-screen
  trick `gui/webview_host.py` already uses on Windows.

**Two real platform differences found, would need designing around
before any actual porting:**
- **No `wxEVT_WEBVIEW_CREATED` event exists on this GTK wx build**
  (confirmed via `hasattr` on `wx.html2`, not just absent from a docs
  page) -- `browser_shim.py`'s Windows readiness-gate (wait for this
  event before trusting `CoreWebView2` exists) has no direct GTK
  equivalent; would need an alternate readiness signal (e.g. trusting
  `WebView.New()` is synchronously ready on GTK, or gating on the first
  real `LOADED`).
- **The WebView's own internal initial `about:blank` navigation fires
  `LOADED` before the caller's real requested URL's own load completes**
  -- confirmed live: called `LoadURL(real_url)` immediately after
  creation, but the first `EVT_WEBVIEW_LOADED` received was for
  `about:blank`, not the real URL. This is the same *class* of
  first-navigation quirk the Windows implementation review already found
  and fixed once (there it was a spurious `ERROR` with
  `CONNECTION_ABORTED`; here it's a spurious `LOADED`) -- the existing
  generation-counter + URL-cross-check fix in `browser_shim.py`'s
  `_on_loaded` would need re-verifying (not assumed working) against
  this GTK-specific manifestation before relying on it.

**Also confirmed working, not just assumed:** `wx.html2.WebView.IsBackendAvailable(WebViewBackendWebKit)`
returns `True`; `WebView.New(backend=WebViewBackendWebKit)` succeeds;
`RunScriptAsync` + the raw `wxEVT_WEBVIEW_SCRIPT_RESULT` event (needs
`wx.PyEventBinder(wx.html2.wxEVT_WEBVIEW_SCRIPT_RESULT)` since, unlike
Windows, there's no `EVT_WEBVIEW_SCRIPT_RESULT` shortcut constant
pre-bound in `wx.html2`'s namespace on this build) correctly round-trips
a JS result string; `wx.UIActionSimulator` constructs and its
`MouseMove`/`MouseClick` calls work under XWayland.

**Caveat, stated plainly**: this was WSLg specifically -- a Wayland
compositor bridging through to the Windows host, not a bare-metal Linux
desktop (X11, or native Wayland on GNOME/KDE/Sway). The result is
genuinely encouraging and not just guessed-at, but it doesn't
automatically generalize to every Linux desktop environment,
particularly around window positioning (Wayland proper is known to
restrict a client's ability to move its own windows in ways X11
doesn't -- WSLg's XWayland layer happens to permit the `SetPosition()`
call used here, but a native Wayland compositor might not). Spike
scripts (throwaway, not committed) live in this session's scratchpad:
`wsl_spike_01_basic.py`, `wsl_spike_02_audio.py`.

**Not yet done, explicitly out of scope for this spike**: window
close/recreate cycling (the Windows spike's third sub-test), a real
macOS spike (no macOS environment available in this session at all,
unlike Linux via WSL), and any actual porting code -- per this project's
established practice, a spike result isn't authorization to start
implementing; that needs its own plan -> review -> implement -> review
cycle, decided with the user when there's appetite for a real
cross-platform round.

## v9 punch list -- from live user testing (2026-08-07) -- DONE

The user tested the freshly-built Windows exe (see "Packaged Windows
release" below) and reported 8 real issues from actual use, not code
reading. Each was re-traced against the actual current code before
fixing (per this project's established practice), then implemented,
independently reviewed (Opus pass), fixed, and re-verified.

1. **App icon** -- generated a simple antenna/radio-wave `.ico`
   (`sdrsync/icon.ico`, Pillow, 16-256px), wired via new
   `sdrsync/resources.py` (`ICON_PATH`, `sys._MEIPASS`-aware for frozen
   builds) into both `gui/app.py`'s `MainFrame` and
   `gui/webview_host.py`'s `WebViewHost`. **Review caught a real
   blocker**: the frozen-build path resolved to `_MEIPASS/icon.ico` but
   `--add-data "sdrsync/icon.ico;sdrsync"` places it at
   `_MEIPASS/sdrsync/icon.ico` -- fixed the path, and hardened both
   `SetIcon()` call sites to check `ICON_PATH.exists()`/`icon.IsOk()`
   before calling `SetIcon` (a missing/bad path doesn't raise, it can
   pop a blocking wx modal error dialog with no log output -- confirmed
   by reproducing the hang). Re-verified via a real PyInstaller rebuild:
   `dist/SDRSync/_internal/sdrsync/icon.ico` exists at the expected path
   and the running frozen exe's log shows no icon-related warning.
2. **Rig disconnect now also stops an active WebSDR session** --
   `SyncEngine._stop_rig()` gained an `if self._websdr_active: await
   self._stop_websdr()` cascade, hit by every path that ends a rig
   session (manual disconnect, the new 30s give-up timeout below, and
   whole-session shutdown). Deliberate, one-directional exception to the
   v3.6 "independent lifecycles" principle -- documented in the code as
   intentional, not a regression of that principle (which was about not
   killing rig when switching WebSDR sites).
3/4. **New, always-available "Disconnect WebSDR" button** -- root cause
   was that the old single Connect/Switch/Disconnect button's Disconnect
   behavior only worked if the dropdown selection still matched the
   active site; once it drifted, there was no way to disconnect (which
   also explained #4 -- `headless_check` stays correctly disabled while
   active, but the user couldn't ever get back to a disconnectable
   state). Fixed with a genuinely separate button, decoupled from
   dropdown state, enabled only while active.
5. **flrig mode-mapping gaps + log-spam fix** -- `DATA-U`/`DATA-L` ->
   USB/LSB and `CW-U`/`CW-L` -> plain CW added to all three drivers'
   `_MODE_MAP` tables (`kiwisdr.py`, `websdr_org.py`, `openwebrx.py`).
   Root cause of "whole program gets slow": `set_mode()`'s "no
   equivalent" warning logged unconditionally on every failed retry (5x/
   sec at default poll interval) -- now rate-limited (log once, then
   downgrade repeats to debug), mirroring `openwebrx.py`'s existing
   out-of-range-frequency precedent. **Caveat still stands**: the actual
   "slow to respond" symptom can't be re-confirmed without real flrig
   hardware, only that this is a real, now-bounded root cause.
6. **Rig connect timeout** -- new `RIG_CONNECT_TIMEOUT_S = 30.0`;
   `_tick()` now gives up and calls `_stop_rig(error=...)` with an
   explanatory message if the deadline passes before first connect,
   instead of retrying forever at "connecting...". Deadline clears on
   first successful connect so a later transient reconnect doesn't
   inherit a stale budget.
7. **Rig-connected fields locked down** -- `port_entry` now disabled
   alongside `mock_rig_check`/`host_entry`/`rig_backend_combo` while
   connected. **`poll_interval_ctrl` deliberately NOT locked** (a
   correction to the original punch-list item, caught by the
   implementation review): unlike the other 4 fields, poll interval is
   read live every tick by `_poll_loop`, so it's a genuinely safe,
   already-working mid-session live-tunable knob -- locking it would
   have been a real regression, not hardening.
8. **Curated site list auto-fetches on first run** -- per the user's
   explicit choice (over a bundled-snapshot alternative), `MainFrame.
   __init__` now kicks off one background "Update from GitHub" fetch if
   `settings.curated_sites` is empty, reusing `SiteManagerDialog.
   _run_fetch`. Silent on failure (no error dialog -- this is an
   unrequested convenience fetch, unlike the dialog's own button-
   triggered fetches).

**Implementation review (independent Opus pass)** found 2 more real
issues beyond the icon-path blocker above, both fixed and re-verified:
the new Disconnect button's label could flicker back to "Disconnecting..."
after being re-enabled (a queued stale snapshot landing before the
engine's stop actually completed) -- fixed by explicitly resetting the
label in the active branch of `_update_websdr_controls`, same as the
connect button already does. Full details of what was checked and
cleared (the stop-cascade's every call site, the timeout deadline's
reset lifecycle, the mode-map rate-limiting reset parity, the auto-fetch
thread's timing against the dispatch table, the button-split's leftover
references) are in this session's transcript, not reproduced here.

`pytest` -- 135/135 passing after all fixes. GUI-heavy items (3/4/7/8)
verified via standalone wx-API checks and manual click-through, per this
project's established practice for GUI-wiring rounds with no headless-wx
test harness.

## Packaged Windows release

**v1.0.0 Windows build -- DONE, published** (2026-08-07,
`github.com/Ar0xA/sdrsync/releases/tag/v1.0.0`). PyInstaller `--onedir`
build (`--collect-all wx` to pull in `WebView2Loader.dll` and the rest of
wx's runtime DLLs, which aren't found by default module-graph analysis),
zipped as `SDRSync-v1.0.0-win64.zip` (~37MB), attached as a GitHub
Release asset -- no installer, extract and run `SDRSync.exe`. Verified
for real, not just "it compiled": ran the frozen exe and confirmed via
`sdrsync.log` that it actually starts (`SDRSync 1.0.0 starting`),
detects the WebView2 backend, and starts the sync engine, before killing
the test process. `build/`, `dist/`, `*.spec` added to `.gitignore` (were
previously untracked but not excluded) since these are machine-generated
packaging output, not source -- release zips are attached to GitHub
Releases instead of committed.

**Build command (as of v9's app-icon addition)** -- the icon flags are
required, not optional, since `sdrsync/resources.py` expects
`icon.ico` at `_MEIPASS/sdrsync/icon.ico` in a frozen build:
```
pyinstaller --onedir --name SDRSync --collect-all wx \
  --icon sdrsync/icon.ico --add-data "sdrsync/icon.ico;sdrsync" \
  --windowed sdrsync/main.py
```
`--icon` embeds the exe's own shell-visible resource icon (taskbar/
Explorer); `--add-data` separately bundles the raw file so `wx.Icon()`
can load it at runtime via `resources.ICON_PATH` -- confirmed during v9
that `--icon` alone does not also satisfy the second need.

**v1.0.1 -- DONE, published** (2026-08-07,
`github.com/Ar0xA/sdrsync/releases/tag/v1.0.1`). Straight repackage of
the v9 punch-list fixes above -- no code changes beyond the version bump,
same build command.

## v9.1 -- two more user-reported issues (2026-08-07) -- DONE, released as v1.0.2

1. **`websdr_connect_btn` label clipped ("...witch WebSDR")**. Root cause,
   confirmed by reading the actual `GridBagSizer` layout in
   `_build_websdr_panel`: the button's column width is computed once, at
   `SetSizerAndFit()` time in `_build_widgets`, from whatever label is
   showing then (`"Connect"`) -- relabeling to `"Switch WebSDR"` later
   (`_update_websdr_controls`) doesn't retrigger that sizing, so the wider
   text got clipped. **First attempted fix was wrong and reverted**: simply
   padding the whole frame's width after `Fit()` doesn't help, since only
   column 1 (the site dropdown) is growable (`AddGrowableCol(1)`) -- extra
   frame width flows there, not into the button's non-growable column 3.
   **Real fix**: at button-creation time, temporarily set the label to the
   widest string it's ever relabeled to at runtime (`"Switch WebSDR"`),
   read `GetBestSize()`, `SetMinSize()` to that plus 12px margin, then
   restore the initial `"Connect"` label -- this makes the column reserve
   the right width from the very first `Fit()`, regardless of which label
   happens to be showing. Verified two ways: a standalone `GridBagSizer`
   script confirming the button's rect is wider than the text extent, and
   a full live run (mock rig + mock WebSDR connect, then switching the
   site dropdown) with a real screenshot showing "Switch WebSDR" fully
   rendered with clean padding on both sides.
2. **KiwiSDR-family sites show "USN"/"LSN" instead of "USB"/"LSB"** (user
   confirmed reproducible on ON1AFF's KiwiSDR instance, not on Twente/
   websdr.org). Root cause, confirmed by reading `kiwisdr.py`'s
   `get_status()`: it returns `window.ext_get_mode()`'s raw KiwiSDR
   internal string verbatim (uppercased) -- when the driver had picked the
   narrow-filter variant for a sub-2kHz passband (`map_hamlib_mode_kiwi`'s
   existing, correct, and unchanged narrow-mode logic from v9), that raw
   string is `"usn"`/`"lsn"`, which reads as a different mode than the
   hamlib USB/LSB it actually corresponds to. Fixed by normalizing
   `get_status()`'s displayed mode through the already-existing
   `_base_mode_of()` helper (previously only used internally for CW-offset
   detection) before uppercasing. **Also fixed the same latent bug in
   `websdr_org.py`'s `get_status()`** for consistency/future-proofing --
   `window.mode` can equally legitimately read back `"USBN"`/`"LSBN"` for
   a narrow passband there, it just happened not to reproduce for this
   user's specific session; stripped the trailing `"N"` the same way
   `set_mode()`'s own `self._current_mode` tracking already does.
   `openwebrx.py` has no narrow-suffix convention at all, so it was
   already unaffected and needed no change. New regression tests:
   `tests/test_kiwisdr_status_mode.py`, `tests/test_websdr_org_status_mode.py`
   (stub-page `get_status()` round trips), plus pure-function coverage for
   `_base_mode_of()` added to `tests/test_kiwisdr_mode_mapping.py`.

`pytest` -- 146/146 passing (11 new tests). No independent review pass for
this small, two-item round (unlike the full v9 punch list) -- both fixes
were traced to a confirmed root cause in the actual code before being
made, and both were verified live (screenshot for #1, unit tests +
manual reasoning for #2, no real KiwiSDR/flrig hardware available in this
environment to re-confirm #2's exact symptom end-to-end).

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
