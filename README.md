# SDRSync

An open-source, Python alternative to CatSyncSDR: keeps a WebSDR (a
shortwave receiver you tune through a website) in sync with a real
transceiver over the network, via either hamlib's `rigctld` or `flrig`'s
own XML-RPC interface — **not** Omnirig.

**Sync direction is one-way only: transceiver → WebSDR.** Turning the dial
or changing mode on your radio retunes the WebSDR; the reverse (tuning the
WebSDR to move your radio) is not implemented.

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
    mode: logged, shown in the WebSDR panel, frequency/mode sync elsewhere
    unaffected) rather than silently dropped or retried forever.
  - **OpenWebRX mute-on-TX**: uses the page's own mute toggle
    (`toggleMute()`), confirmed working live — unlike the other two
    drivers this one doesn't need a documented no-op fallback here.
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
    - **Mute-on-TX uses `duck`, not `mute`.** UberSDR separates the
      operator's own mute setting from transient silence applied by a
      controller. Using the transient one means sdrsync dying mid-transmission
      cannot leave somebody's receiver muted for good, and the mute button on
      their page never shows a state they did not choose.
    - **Only the v2 interface has this API.** Paste the receiver's root URL —
      the address operators publish — and the driver navigates to `/v2/`
      itself, preserving any query string so an UberSDR share link lands
      where it says.
    - **If the operator has switched the API off** (their SDR Control panel →
      *Browser bridge*), the page says so explicitly and the WebSDR panel
      reports that, rather than showing a timeout that looks like a broken
      site.
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
independence, and — for UberSDR — the exact commands the driver puts on
the wire, via a stub page) — no browser, socket, or real hardware needed.

## License

GPL-3.0-only — see [LICENSE](LICENSE).

## Platform support

Windows only for now (the embedded browser is Edge WebView2). Linux/macOS
support is a known, tracked gap — not yet started.
