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
over the reverse direction: a **Sync direction** dropdown that can pause
it for the session, and an optional **frequency range guard** that
rejects anything outside a min/max you set. Both are described under
"Reverse sync" below
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

- **Sync direction** — a dropdown in the Behaviour settings panel.
  Choosing **Rig → WebSDR** pauses the reverse direction outright (the
  old "Hold" checkbox, renamed); **Rig ↔ WebSDR (reverse within range)**
  is the default, bidirectional behavior. Forward sync (rig → WebSDR)
  keeps working either way and nothing disconnects — the page simply
  stops being able to move your rig while one-way is selected. It's
  session-only: it does **not** persist, and every launch starts
  bidirectional, so it's a deliberate "for right now" control, not a
  setting you can configure once and forget. A write already on its way
  to the rig when you switch it may still complete once — it can't be
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
safe by itself — one-way sync is off unless you select it, and the range
guard is only as good as the numbers you enter. If you're pointing a real
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
  through your normal audio output device, not a reimplemented codec.
  The receiver is embedded directly in the main window (no separate
  popup), so there's nothing to hide or lose track of.
- **No separate browser install step** — unlike an earlier version of this
  project (which used Playwright/Chromium, requiring a
  `playwright install chromium` download), the embedded WebView2 engine
  ships with Windows 10/11 already, so `pip install -r requirements.txt`
  is the whole setup.
- Each WebSDR *software family* (websdr.org's classic "WebSDR", KiwiSDR,
  OpenWebRX, ...) has its own unrelated control API, so each gets its own
  driver module implementing `sdrsync.websdr.base.WebSDRDriver`. Four
  families are implemented today: websdr.org
  (`sdrsync/websdr/websdr_org.py`), which covers
  `websdr.ewi.utwente.nl:8901` and other sites running the same software;
  KiwiSDR (`sdrsync/websdr/kiwisdr.py`), which covers `*.proxy.kiwisdr.com`
  and other KiwiSDR instances; OpenWebRX/OpenWebRX+
  (`sdrsync/websdr/openwebrx.py`), likely the most common self-hosted
  WebSDR software by receiver count; and UberSDR
  (`sdrsync/websdr/ubersdr.py`).
  - **OpenWebRX limitation**: some OpenWebRX stations run multiple SDR
    "profiles" (e.g. a separate HF device and a separate VHF/UHF device).
    Automatically switching profiles to follow a rig frequency outside the
    *currently active* profile's range isn't implemented yet — a
    frequency outside range is reported clearly (same as an unsupported
    mode: logged, shown in the status bar, frequency/mode sync elsewhere
    unaffected) rather than silently dropped or retried forever.
  - **UberSDR is the exception to the "call the site's own JS" rule**, and
    the only one that is not reverse-engineered. Its v2 interface publishes
    a documented, versioned control API (`static/v2/BRIDGE_API.md` in the
    UberSDR source) for exactly this purpose — the same one its own Chrome
    and Firefox extensions use — so the driver is a client of that API and
    touches no page internals. What that buys, concretely:
    - **Identification is a handshake, not a guess.** The driver sends the
      API's `hello` and the page answers with an `announce` carrying the
      receiver's identity, protocol version and capability list. A page that
      answers *is* a controllable UberSDR; one that does not is not,
      whatever its HTML looks like. (The `<script src>` fingerprint is still
      registered, because the **Detect** button identifies a pasted URL from
      HTML alone without starting a browser.)
    - **Refusals come with reasons.** An out-of-range frequency is rejected
      with "frequency 40000000 is outside 10000–30000000" rather than
      silently clamped, so the driver reports what the receiver said instead
      of comparing a readback and guessing why it differs.
    - **Mode and filter are set in one call**, so the receiver never passes
      audibly through the new mode's default passband on the way to the
      width the rig reported. The rig's passband becomes real filter edges
      (2400 Hz on USB → 50–2450 Hz), not a choice between a "narrow" and a
      "wide" mode string.
    - **Only the v2 interface has this API.** Paste the receiver's root URL —
      the address operators publish — and the driver navigates to `/v2/`
      itself, preserving any query string so an UberSDR share link lands
      where it says.
    - **If the operator has switched the API off** (their SDR Control panel →
      *Browser bridge*), the page says so explicitly and sdrsync surfaces
      that in the status bar, rather than showing a timeout that looks
      like a broken site.
- Pasting an arbitrary WebSDR URL doesn't require knowing which software
  it runs: in the **SITES** settings tab, paste the URL into the Add-a-site
  form and click **Detect** — sdrsync fetches the page's HTML and checks
  its `<script src="...">` tags against each registered driver's known
  marker filename(s) (e.g. `websdr-base.js` for websdr.org,
  `kiwisdr.min.js` for KiwiSDR, `compiled/receiver.js` for OpenWebRX). If
  the site doesn't match any known driver, or matches more than one
  (ambiguous), Detect reports that rather than guessing.
- **The rig connection and the WebSDR connection are two fully
  independent subsystems** — not one combined session. Picking a
  different WebSDR has nothing to do with your transceiver, so it never
  touches the rigctld/flrig connection (or vice versa): connect to your
  rig once (Transceiver settings tab) and leave it running, then freely
  switch which WebSDR you're listening through from the top strip's own
  dropdown — while connected, the same button relabels to **Load** the
  moment you pick a different site, and swaps the embedded receiver's
  content in place (no popup, no flicker, your transceiver connection
  completely untouched); it reads **Disconnect** again once the dropdown
  matches whatever's active. If rigctld/flrig drops, it reconnects with
  backoff while the WebSDR keeps running unaffected, and vice versa if
  the WebSDR page fails to load or becomes incompatible — the status bar
  and each settings tab show their own status and last error
  independently.
- Frequency **and** mode both sync, independently of each other. If your
  rig reports a mode this particular WebSDR driver has no equivalent for,
  frequency sync is unaffected — the GUI just shows a message that that one
  mode isn't supported, and resumes silently once you switch to a
  supported one.
- **Mute on TX** silences the WebSDR's audio the instant your rig keys up
  (mic, CW, footswitch — anything the rig itself reports as PTT, not just
  PTT sdrsync commanded), and unmutes on the falling edge. This mutes the
  whole embedded browser's audio output directly, at the native browser-
  engine level (`webkit_web_view_set_is_muted()` on Linux, WebView2's
  `IsMuted` on Windows) — no per-site JavaScript is involved any more, so
  it's instant and identical across every WebSDR family, rather than
  depending on each site's own mute implementation. Verified live on
  Linux; the Windows path uses the same native API but hasn't been
  live-verified yet.
- **Rig PTT detection depends on your rig software actually polling it
  live**, not just tracking commands it issued itself — both `rigctld`
  and flrig default to this correctly for most modern CAT-capable rigs,
  but two common misconfigurations silently break it: `rigctld` started
  with `-P RTS`/`-P DTR` and a separate PTT-only serial port (that only
  reads back the pin it last set, never the rig's real state — use
  `-P RIG`, the default for most rigs, instead), and flrig's "PTT on CAT
  serial port" needs to be **Both** (not just Get or Set) *and* its own
  "PTT" checkbox in the Polling section needs to be ticked — flrig won't
  actually query live PTT at all without that second setting, regardless
  of the CAT setting. See the `[?]` next to **Mute on TX** in the app for
  the full detail.

## Install

**Windows (recommended):** download the latest `SDRSync-vX.Y.Z-windows.zip` from
[Releases](https://github.com/Ar0xA/sdrsync/releases), unzip it, and run `SDRSync.exe` — no
Python needed.

**Running from source (Windows or macOS):**

```bash
pip install -r requirements.txt
```

Or, as an editable install (also registers an `sdrsync` command):

```bash
pip install -e .
```

**Linux:** `pip install`ing wxPython on Linux falls back to a slow,
often-failing source build — PyPI ships no manylinux wheels for it.
`requirements.txt`/`pyproject.toml` both skip wxPython on Linux for this
reason; install your distro's package instead, *then* install the rest:

```bash
# Debian/Ubuntu -- package names confirmed working during this project's
# own testing (Debian 12):
sudo apt install python3-wxgtk4.0 python3-wxgtk-webview4.0 libwebkit2gtk-4.1-0
pip install -r requirements.txt   # skips wxPython automatically on Linux
```

On another distro, install its equivalent wxPython + WebView (WebKitGTK
4.1) packages. If sdrsync still can't find a working WebView backend at
startup, the error message names the exact packages it's missing.

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
2. Start sdrsync — double-click `SDRSync.exe` if you're on the packaged Windows build, or from
   source:
   ```bash
   python -m sdrsync.main
   ```
3. **Transceiver settings** (click the **TRANSCEIVER** tab under the top
   strip) has its own **Test**/**Connect** buttons. **Test** checks
   reachability (a rigctld TCP connect or a flrig XML-RPC probe,
   depending on the selected backend) without actually connecting.
4. Once the transceiver is connected, pick a site from the dropdown in
   the top strip and click **Connect** there (only enabled once the
   transceiver is actually connected, not just mid-handshake) — the
   receiver embeds directly in the main window, no separate popup.
   To switch to a different WebSDR later, pick a new site in the
   dropdown and click the same button — it relabels to **Load** and
   swaps the embedded receiver's content in place, without
   disconnecting or reopening anything. Picking the currently-active
   site back in the dropdown relabels the button to **Disconnect**,
   which ends the WebSDR side only — use the Transceiver settings'
   own button to end the rig connection. The dropdown restores
   whichever site you last connected to on the next launch.
5. To add a site that isn't already in the dropdown, open the **SITES**
   tab: fill in a Name and URL, click **Detect** to identify which
   WebSDR software it runs, then **Save to list** (or **Test** first to
   check it's reachable). The **More ▾** menu next to the saved-sites
   list can also load a site list from a local file or a URL, or
   refresh the curated list from GitHub.

### Idle disconnect

By default, sdrsync disconnects the WebSDR session after 60 minutes of
no rig activity at all (no frequency/mode/PTT change) and reconnects
automatically the instant you touch the rig again — the status bar
shows this as a paused/idle state, not an error. This isn't a bug: most
WebSDR receivers are volunteer-run, shared infrastructure with a small
number of concurrent-listener slots, and holding one open indefinitely
while you've stepped away from the radio denies it to someone else.
Change the threshold, or set it to **0** to disable it and hold the
connection forever, via the **Idle disconnect (min)** field in the
**BEHAVIOUR** settings tab.

### Poll interval

The Transceiver panel's **Poll interval (s)** control sets how often
sdrsync reads your rig's frequency/mode/PTT and pushes changes to the
WebSDR (default 0.2s / 5 times a second). Lower it further for snappier
tracking, or raise it if you're on a slow CAT link (e.g. low-baud-rate
serial) or sharing `rigctld` with other software and want to reduce CAT
traffic. Takes effect immediately, no reconnect needed.

### Testing without real hardware

Check **Use mock rig (embedded, for testing)** in the Transceiver
settings tab before connecting: sdrsync starts its own tiny in-process
server speaking whichever backend is selected (rigctld's line protocol,
or flrig's XML-RPC interface) -- bound to the host/port you'd otherwise
point at the real thing, host field forced to `127.0.0.1` and locked
while this is checked -- and a **Mock Rig Control** panel appears below
it once the rig is connected, letting you set frequency, mode +
passband, and PTT directly from the app and watch the embedded WebSDR
follow. Its freq/mode/PTT fields stay disabled until a WebSDR is also
connected (there's nothing for them to reach before then); the panel
only ever appears in mock mode at all, so a real-rig run never shows
controls that look like they drive a real radio.

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
   GitHub — see the Sites settings tab's **More ▾ → Update from GitHub**).
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
independence, and — for UberSDR — the exact commands the driver puts on
the wire, via a stub page) — no browser, socket, or real hardware needed.

## License

GPL-3.0-only — see [LICENSE](LICENSE).

## Platform support

- **Windows**: fully supported, packaged builds released as zips (Edge
  WebView2 as the embedded browser) — see
  [Releases](https://github.com/Ar0xA/sdrsync/releases) for a ready-to-run
  `SDRSync-vX.Y.Z-windows.zip` (unzip, run `SDRSync.exe`, no Python
  needed).
- **Linux**: supported, but currently **run from source only** — no
  packaged tarball has shipped since v1.1.0. Install the distro
  wxPython/WebKitGTK packages (see "Install" above), then launch the
  same way as any platform:
  ```bash
  python -m sdrsync.main
  ```
  (WebKitGTK is the embedded browser.) Live-verified inside WSL2/WSLg and,
  as of v2.2.7, on a bare-metal Linux desktop (Linux Mint/Cinnamon, X11) —
  the latter needed a fix for WebSDR audio staying silent there (WebKitGTK
  reports a non-standard `AudioContext` state, `"interrupted"`, that the
  WSL2-only-tested audio-unlock code didn't handle; see `project_brief.md`).
  Still not run on a non-GNOME/non-Cinnamon/non-XWayland compositor.
- **macOS**: best-effort code path only, **never run on an actual Mac**
  — no Mac has been available during development. Treat as unverified.
