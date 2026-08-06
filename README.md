# SDRSync

An open-source, Python alternative to CatSyncSDR: keeps a WebSDR (a
shortwave receiver you tune through a website) in sync with a real
transceiver over the network via hamlib's `rigctld` — **not** Omnirig.

**Sync direction is one-way only: transceiver → WebSDR.** Turning the dial
or changing mode on your radio retunes the WebSDR; the reverse (tuning the
WebSDR to move your radio) is not implemented.

## How it works

Rather than reimplementing a given WebSDR site's custom binary
websocket/audio protocol, sdrsync launches a real Chromium browser tab (via
Playwright) pointed at the actual WebSDR page and drives it by calling the
site's own JavaScript control functions. This means:

- **Audio just works** — it's genuine audio from a genuine browser tab
  through your normal audio output device, not a reimplemented codec. This
  is true even with the browser window hidden (see "Hidden window mode"
  below); Chromium's actual `--headless` mode has no audio output at all
  (confirmed by testing), so sdrsync never uses it.
- Each WebSDR *software family* (websdr.org's classic "WebSDR", KiwiSDR,
  OpenWebRX, ...) has its own unrelated control API, so each gets its own
  driver module implementing `sdrsync.websdr.base.WebSDRDriver`. Three
  families are implemented today: websdr.org
  (`sdrsync/websdr/websdr_org.py`), which covers
  `websdr.ewi.utwente.nl:8901` and other sites running the same software;
  KiwiSDR (`sdrsync/websdr/kiwisdr.py`), which covers `*.proxy.kiwisdr.com`
  and other KiwiSDR instances; and OpenWebRX/OpenWebRX+
  (`sdrsync/websdr/openwebrx.py`), likely the most common self-hosted
  WebSDR software by receiver count.
  - **OpenWebRX limitation**: some OpenWebRX stations run multiple SDR
    "profiles" (e.g. a separate HF device and a separate VHF/UHF device).
    Automatically switching profiles to follow a rig frequency outside the
    *currently active* profile's range isn't implemented yet — a
    frequency outside range is reported clearly (same as an unsupported
    mode: logged, shown in the WebSDR panel, frequency/mode sync elsewhere
    unaffected) rather than silently dropped or retried forever.
  - **OpenWebRX mute-on-TX**: uses the page's own mute toggle
    (`toggleMute()`), confirmed working live — unlike the other two
    drivers this one doesn't need a documented no-op fallback here.
- Pasting an arbitrary WebSDR URL doesn't require knowing which software
  it runs: pick **Custom URL...** in the site dropdown, paste the URL, and
  click **Detect** — sdrsync fetches the page's HTML and checks its
  `<script src="...">` tags against each registered driver's known marker
  filename(s) (e.g. `websdr-base.js` for websdr.org, `kiwisdr.min.js` for
  KiwiSDR, `compiled/receiver.js` for OpenWebRX). If the site doesn't
  match any known driver, or matches more than one (ambiguous), Detect
  reports that rather than guessing.
- **The rig connection and the WebSDR connection are two fully independent
  subsystems, each with its own Connect/Disconnect in its own panel** —
  not one combined session. Picking a different WebSDR has nothing to do
  with your transceiver, so it never touches the rigctld connection (or
  vice versa): connect to your rig once and leave it running, then freely
  switch which WebSDR you're listening through — clicking Connect in the
  WebSDR panel while another site is already loaded just replaces it
  (relabeled **Switch WebSDR** when that's what it'll do). If rigctld
  drops, it reconnects with backoff while the WebSDR keeps running
  unaffected, and vice versa if the WebSDR page fails to load or becomes
  incompatible — each panel shows its own status and last error
  independently.
- Frequency **and** mode both sync, independently of each other. If your
  rig reports a mode this particular WebSDR driver has no equivalent for,
  frequency sync is unaffected — the GUI just shows a message that that one
  mode isn't supported, and resumes silently once you switch to a
  supported one.

## Install

```bash
pip install -r requirements.txt
playwright install chromium
```

## Run

1. Point hamlib's `rigctld` at your radio, e.g.:
   ```bash
   rigctld -m <hamlib-model-number> -r <serial-port> -t 4532
   ```
   (Or check **Use mock rig** in the Transceiver panel instead — see below
   — if you just want to try it out without a radio.)
2. Start sdrsync:
   ```bash
   python -m sdrsync.main
   ```
3. The **Transceiver** panel and the **WebSDR** panel each have their own
   independent **Connect**/**Test** buttons — use them in either order.
   **Test** checks reachability (rigctld TCP connect, or an HTTP GET to the
   WebSDR URL) without actually connecting/launching a browser.
4. In the WebSDR panel, pick a site from the dropdown (or **Custom URL...**
   + **Detect**) and click **Connect**. To switch to a different WebSDR
   later, just pick a new site and click the same button again — it's now
   labeled **Switch WebSDR** and swaps only the browser tab/driver, leaving
   your transceiver connection completely untouched. **Disconnect** only
   appears when the *currently selected* site is the one that's active, and
   only ends the WebSDR side — use the Transceiver panel's own Disconnect
   to end the rig connection.

### Hidden window mode

Check **Hide browser window (audio still plays)** before connecting to run
without a visible Chromium window on screen. Despite the setting's internal
name (`headless` in `config.py`), this does **not** use Chromium's real
`--headless` flag — that mode has no audio output at all, full stop,
regardless of which Chromium binary or launch flags are used (tested both
the lightweight "headless shell" and the full binary's own headless mode).
Instead, sdrsync launches a normal, fully headed browser and positions its
window far off any screen (`--window-position=-32000,-32000`), which keeps
the real audio pipeline intact while showing nothing on screen.

### Testing without real hardware

Check **Use mock rig (embedded, for testing)** before connecting: sdrsync
starts its own tiny rigctld-compatible server in-process (bound to the
host/port you'd otherwise point at a real rigctld — the host field is
forced to `127.0.0.1` and locked while this is checked) and a **Mock Rig
Control** panel appears once connected, letting you set frequency, mode +
passband, and PTT directly from the app and watch the (real, headed)
WebSDR tab follow — no separate terminal or real radio needed. The panel
only ever appears in mock mode, so a real-rig run never shows controls
that look like they drive a real radio.

`sdrsync/rig/fake_rigctld.py` also still works standalone, if you'd rather
drive it from a terminal instead of the GUI panel:

```bash
python -m sdrsync.rig.fake_rigctld
```

It listens on `127.0.0.1:4532` and gives you a small prompt (`f <hz>`,
`m <mode> <passband_hz>`, `t <0|1>`) to simulate the radio changing state.

## Adding a new WebSDR type

1. Implement `sdrsync.websdr.base.WebSDRDriver` in a new module under
   `sdrsync/websdr/`, including a `FINGERPRINT_MARKERS: ClassVar[tuple[str, ...]]`
   class attribute — one or more distinctive `<script src="...">` filename
   substrings that only this software family's pages ever load (used by
   the Custom URL Detect flow; see `sdrsync/websdr/registry.py`).
2. Register it in `sdrsync/websdr/registry.py`'s `DRIVERS` dict under a new
   `driver_type` key.
3. Add a `WebSDRSite(name=..., url=..., driver_type=...)` entry to
   `KNOWN_SITES` in `sdrsync/config.py`.

## Tests

```bash
pytest
```

Covers pure logic and stubbed-object engine behavior only (mode mapping,
band selection, rigctld response parsing, mode/frequency-sync
independence) — no browser, socket, or real hardware needed.
