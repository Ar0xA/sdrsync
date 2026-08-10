# SDRSync

An open-source, Python alternative to CatSyncSDR: keeps a WebSDR (a
shortwave receiver you tune through a website) in sync with a real
transceiver over the network, via either hamlib's `rigctld` or `flrig`'s
own XML-RPC interface — **not** Omnirig.

**Sync is bidirectional.** Turning the dial or changing mode on your radio
retunes the WebSDR, and clicking/tuning directly on the WebSDR page moves
your radio's frequency and mode right back. **This means the WebSDR page
can retune your transmitter.** PTT/transmit always wins (rig state is
never overridden while you're transmitting), and there are two controls
over the reverse direction: a **Hold** toggle that pauses it for the
session, and an optional **frequency range guard** that rejects anything
outside a min/max you set. Both are described under "Reverse sync" below
— read it before connecting a real transceiver to a public WebSDR you
don't control, because neither of them is a band-plan or legality check.

## Reverse sync (WebSDR → rig)

Changing frequency or mode on the WebSDR page itself (clicking the
waterfall, using the page's own mode buttons) pushes that change to your
rig via `rigctld`/`flrig`'s SET commands, verified by reading the change
back from the rig before trusting it. This is suppressed the whole time
you're transmitting, so PTT always wins — the WebSDR can't move your VFO
mid-transmission.

Two controls sit on top of that, both in your hands:

- **Hold (WebSDR read-only)** — a toggle in the WebSDR panel that pauses
  the reverse direction outright. Forward sync (rig → WebSDR) keeps
  working and nothing disconnects; the page simply stops being able to
  move your rig. It's session-only: it does **not** persist, and every
  launch starts with it off, so it's a deliberate "for right now" control,
  not a setting you can configure once and forget. A write already on its
  way to the rig when you press it may still complete once — it can't be
  recalled mid-send.
- **Reverse-sync range (Hz)** — an optional min/max in the Transceiver
  panel. A reverse push whose rig-side frequency falls outside it is
  rejected outright (no retries) and the WebSDR is reverted to match the
  rig. It's opt-in and **defaults to unrestricted**: leave either box
  blank for no bound on that side, so unless you fill it in, nothing is
  restricted. Setting it to your rig's usable range (e.g. 1800000 to
  30000000) is a reasonable precaution against a stray waterfall click on
  a wideband receiver.

Beyond those, there is still **no other bound** on what gets sent: no
band-plan check, no legality check for your licence or country, no "is
this frequency even valid for my rig" check, no confirmation before a
large jump inside an allowed range. Neither control makes reverse sync
safe by itself — Hold is off unless you press it, and the range guard is
only as good as the numbers you enter. If you're pointing a real
transceiver at a public WebSDR you don't control, be aware of this before
leaving it connected unattended.

**Testing status, plainly**: reverse sync has been live-tested against
real hardware on websdr.org and KiwiSDR, through both the `rigctld` and
`flrig` backends. It has **not** yet been tested against real hardware
on OpenWebRX — that's the one remaining untested combination.

## How it works

Rather than reimplementing a given WebSDR site's custom binary
websocket/audio protocol, sdrsync embeds a real browser view (your OS's
native web engine — Edge WebView2 on Windows, via wxPython) pointed at the
actual WebSDR page and drives it by calling the site's own JavaScript
control functions. This means:

- **Audio just works** — it's genuine audio from a genuine browser engine
  through your normal audio output device, not a reimplemented codec. This
  is true even with the browser window hidden (see "Hidden window mode"
  below).
- **No separate browser install step** — unlike an earlier version of this
  project (which used Playwright/Chromium, requiring a
  `playwright install chromium` download), the embedded WebView2 engine
  ships with Windows 10/11 already, so `pip install -r requirements.txt`
  is the whole setup.
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
```

Or, as an editable install (also registers an `sdrsync` command):

```bash
pip install -e .
```

## Run

1. Point a rig-control backend at your radio — the Transceiver panel's
   **Backend** dropdown picks which one:
   - **rigctld** (hamlib): `rigctld -m <hamlib-model-number> -r <serial-port> -t 4532`
   - **flrig**: start flrig normally and enable its XML-RPC server
     (`Config > Setup > Transceiver > Server`, default port 12345 --
     see [flrig's docs](http://www.w1hkj.com/flrig-help/xmlrpc_server.html)).
   Each backend keeps its own host/port, so switching the dropdown back
   and forth doesn't lose either one's settings.
   (Or check **Use mock rig** in the Transceiver panel instead — see below
   — if you just want to try it out without a radio; works with either
   backend.)
2. Start sdrsync:
   ```bash
   python -m sdrsync.main
   ```
3. The **Transceiver** panel and the **WebSDR** panel each have their own
   independent **Connect**/**Test** buttons — use them in either order.
   **Test** checks reachability (a rigctld TCP connect or a flrig XML-RPC
   probe, depending on the selected backend; an HTTP GET for the WebSDR
   URL) without actually connecting/launching a browser.
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
without a visible browser window on screen. sdrsync keeps its WebView
window positioned far off any screen while genuinely shown (not
minimized/hidden, which suspends WebView2's rendering and audio) —
this keeps the real audio pipeline intact while showing nothing on
screen.

### Window size, position, and idle behavior

The WebSDR browser window remembers its own size and position
separately per WebSDR *software family* (websdr.org, KiwiSDR, OpenWebRX,
...), restoring it the next time you connect to that type — a KiwiSDR
window you left large on a second monitor comes back there, independent
of wherever you last left an OpenWebRX window. The very first time a
given type has nothing remembered yet, it opens on whichever monitor
sdrsync's own main window is currently on. The main sdrsync window
itself also remembers its own size and position across restarts, the
same way. In both cases, if the remembered position was on a monitor
that's no longer connected (a laptop undocked, a display unplugged
since), it falls back to whatever monitor is available instead of
landing somewhere unreachable.

When there's no active WebSDR connection at all — at startup, or after a
disconnect — the WebSDR browser window is hidden outright (not just
moved off-screen) rather than sitting empty on screen or in the
taskbar. It reappears, positioned above the main window, the moment you
connect. Clicking the WebSDR window's own close (X) button disconnects
that session, the same as the **Disconnect WebSDR** button — it doesn't
just refuse to close.

### Idle disconnect

By default, sdrsync disconnects the WebSDR session after 60 minutes of
no rig activity at all (no frequency/mode/PTT change) and reconnects
automatically the instant you touch the rig again — the panel shows
**"disconnected (idle)"** when this happens, not an error. This isn't a
bug: most WebSDR receivers are volunteer-run, shared infrastructure with
a small number of concurrent-listener slots, and holding one open
indefinitely while you've stepped away from the radio denies it to
someone else. Change the threshold, or clear the field (or set it to 0)
to disable it and hold the connection forever, via the **Idle disconnect
(min)** field in the WebSDR panel.

### Poll interval

The Transceiver panel's **Poll interval (s)** control sets how often
sdrsync reads your rig's frequency/mode/PTT and pushes changes to the
WebSDR (default 0.2s / 5 times a second). Lower it further for snappier
tracking, or raise it if you're on a slow CAT link (e.g. low-baud-rate
serial) or sharing `rigctld` with other software and want to reduce CAT
traffic. Takes effect immediately, no reconnect needed.

### Testing without real hardware

Check **Use mock rig (embedded, for testing)** before connecting: sdrsync
starts its own tiny in-process server speaking whichever backend is
selected (rigctld's line protocol, or flrig's XML-RPC interface) --
bound to the host/port you'd otherwise point at the real thing, host
field forced to `127.0.0.1` and locked while this is checked -- and a
**Mock Rig Control** panel appears once connected, letting you set
frequency, mode + passband, and PTT directly from the app and watch the
(real, headed) WebSDR tab follow — no separate terminal or real radio
needed. The panel only ever appears in mock mode, so a real-rig run
never shows controls that look like they drive a real radio.

`sdrsync/rig/fake_rigctld.py` also still works standalone, if you'd rather
drive it from a terminal instead of the GUI panel:

```bash
python -m sdrsync.rig.fake_rigctld
```

It listens on `127.0.0.1:4532` and gives you a small prompt (`f <hz>`,
`m <mode> <passband_hz>`, `t <0|1>`) to simulate the radio changing state.

## Adding a new WebSDR type

1. Implement `sdrsync.websdr.base.WebSDRDriver` in a new module under
   `sdrsync/websdr/` — read that Protocol's docstrings first, not just its
   method signatures; every method there documents a real constraint an
   earlier driver got wrong before the comment was added (e.g. `tune_hz`'s
   `verify` parameter, and why a mode mapper can't just collapse every
   hamlib mode onto the nearest string). **Every method on the Protocol is
   required**, including the two reverse-sync ones added in v11
   (`hamlib_mode_from_status`, `rig_freq_from_status`) — the sync engine
   calls them unconditionally on every tick once a WebSDR is connected,
   with no `hasattr` guard, so a driver missing either one crashes the
   *entire* engine thread (both the WebSDR and rig connections) the
   moment reverse sync ties, not just fails to sync. This is not
   hypothetical: it's exactly the shape of bug an external driver
   contribution hit, because the Protocol had gained both that parameter
   and those two methods since the branch was started — if you're
   picking this project back up after a while, or reviewing someone
   else's driver PR, diff `sdrsync/websdr/base.py` against whatever the
   driver was actually written against before assuming it's complete.
   Also include a `FINGERPRINT_MARKERS: ClassVar[tuple[str, ...]]` class
   attribute — one or more distinctive `<script src="...">` filename
   substrings that only this software family's pages ever load (used by
   the Custom URL Detect flow; see `sdrsync/websdr/registry.py`).
2. Register it in `sdrsync/websdr/registry.py`'s `DRIVERS` dict under a new
   `driver_type` key.
3. Add a `WebSDRSite(name=..., url=..., driver_type=...)` entry to
   `KNOWN_SITES` in `sdrsync/config.py`, and optionally to
   `sites/websdr_sites.json` (the curated list the app can auto-fetch from
   GitHub — see the "Manage sites..." dialog).
4. Add tests following the existing per-driver pattern (e.g.
   `tests/test_kiwisdr_mode_mapping.py`, `tests/test_kiwisdr_reverse_mode_mapping.py`):
   pure hamlib-mode-mapping tests for both directions, and a stub-page test
   that pins the exact commands/JS your driver sends (a fake object
   standing in for the browser page — see any existing driver's test file
   for the shape). Extend `tests/test_fingerprint.py` with detection cases
   for your new marker(s), including a case confirming it does *not*
   false-positive against another driver's fingerprint or an unrelated
   page. There's no way to click through a live WebSDR session in CI or
   in this kind of automated environment, so these pure/stubbed tests are
   the only safety net a driver gets before someone tests it by hand
   against a real receiver.

## Tests

```bash
pytest
```

Covers pure logic and stubbed-object engine behavior only (mode mapping,
band selection, rigctld response parsing, mode/frequency-sync
independence) — no browser, socket, or real hardware needed.

## License

GPL-3.0-only — see [LICENSE](LICENSE).

## Platform support

- **Windows**: fully supported, packaged builds released as zips (Edge
  WebView2 as the embedded browser).
- **Linux**: supported, packaged builds also released as tarballs
  (WebKitGTK as the embedded browser). Live-verified inside WSL2/WSLg
  only — not yet run on a bare-metal Linux desktop or a non-GNOME/non-
  XWayland compositor.
- **macOS**: best-effort code path only, **never run on an actual Mac**
  — no Mac has been available during development. Treat as unverified.
