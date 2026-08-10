# SDRSync — wxPython implementation spec

Implementation brief for the SDRSync desktop UI (Windows + Linux, wxPython/Phoenix ≥ 4.2).
This describes **layout, widgets, sizers, state and behaviour**. Follow it literally; where a
value is given in px it is a DIP (`wx.Window.FromDIP`) unless stated.

---

## 0. Design intent

- The WebSDR page is the product. All chrome is thin and horizontal; the receiver gets every
  remaining pixel.
- Configuration is collapsible and folds away once the rig is connected. It never occupies
  vertical space unless the user opens it.
- Palette is quiet: near-white ground, hairline rules, a single gold accent used as *stroke*
  (borders, underlines, text), never as a filled block. No solid-filled buttons.
- Numbers are the data. Frequencies are always shown with thousands separators
  (`14.074.500`) and in a font with lining tabular figures so digits do not jitter.

---

## 1. Tokens

```python
# colors.py
BG          = wx.Colour(0xF3, 0xF2, 0xF2)   # window ground
SURFACE     = wx.Colour(0xEA, 0xE9, 0xE9)   # title bar / status bar fill
TEXT        = wx.Colour(0x20, 0x1F, 0x1D)
MUTED       = wx.Colour(0x7D, 0x79, 0x79)   # labels, secondary text
FAINT       = wx.Colour(0x9B, 0x97, 0x97)   # disabled / offline
DIVIDER     = wx.Colour(0xD2, 0xD0, 0xCE)   # 1px hairlines
BORDER      = wx.Colour(0xBA, 0xB6, 0xB6)   # control outlines
ACCENT      = wx.Colour(0xB6, 0x82, 0x35)   # gold — strokes and marks only
ACCENT_TEXT = wx.Colour(0x7D, 0x54, 0x11)   # gold at body size (contrast-safe)
ACCENT_TINT = wx.Colour(0xFF, 0xF3, 0xE4)   # hover / checked fill
PANEL_ALT   = wx.Colour(0xF8, 0xF4, 0xF4)   # section bar + status bar
```

Fonts — ship the TTFs with the app and load with `wx.Font.AddPrivateFont`:

| Role | Face | Size | Notes |
| --- | --- | --- | --- |
| Frequencies, mode, values | Lora | 10 pt | needs `tnum`+`lnum`; if the platform cannot enable OpenType features, substitute **DejaVu Sans Mono** or **Cascadia Mono** at 10 pt — tabular lining digits are non-negotiable |
| Big frequency (compact bar) | Cormorant Garamond | 22 pt | regular weight, not bold |
| Labels / kickers | Lora | 7.5 pt | ALL CAPS, letter-spacing faked with `" ".join()` if needed, colour `MUTED` |
| Headings (idle screen) | Cormorant Garamond | 26 pt | regular weight |
| Body (idle screen) | Lora | 10 pt | colour `MUTED` |

Spacing scale: 5 / 9 / 14 / 18 / 28 px. Control corner radius 4 px. Never use bold.

---

## 2. Frame geometry

```
MainFrame (wx.Frame)
  size      1360 × 812
  min size  1100 × 640
  style     wx.DEFAULT_FRAME_STYLE
  title     "SDRSync 1.2.7 — {route}"     route: "not connected" | "rigctld → WebSDR" | "sync paused"
```

Root sizer is `wx.BoxSizer(wx.VERTICAL)` with, top to bottom:

| # | Child | Height | Proportion |
| --- | --- | --- | --- |
| 1 | `StripPanel` | 46 | 0 |
| 2 | `SectionBar` | 30 | 0 |
| 3 | `SettingsHost` | variable, hidden by default | 0 |
| 4 | `ReceiverHost` | — | **1** |
| 5 | `StatusBarPanel` | 28 | 0 |

Every one of 1–3 and 5 draws a 1px `DIVIDER` line on its bottom (or top, for 5) edge in its
`EVT_PAINT`. Do not use `wx.StaticLine` with default colours — they render as OS grey.
Use native `wx.StatusBar`? **No** — draw `StatusBarPanel` yourself so the accent dot and the
log link can be styled.

---

## 3. StripPanel (height 46)

Horizontal `wx.BoxSizer`, 14 px left/right padding, 11 px between groups, all children
vertically centred. Order:

1. **Rig dot** — 6 px owner-drawn circle. Outline `ACCENT` filled `ACCENT` when connected and
   syncing; outline `MUTED` filled `MUTED` when connected but paused; outline `FAINT`
   unfilled when disconnected.
2. **RX VFO group** — `wx.StaticText` label `"RX VFO"` (label font) + value (value font).
3. **TX VFO group** — label `"TX VFO"`, or `"TX VFO (not synced)"` when *Sync TX VFO* is off.
   Value colour: `TEXT` normally, `ACCENT_TEXT` while transmitting, `FAINT` when not synced.
4. **Mode group** — label `"MODE"` + value (`USB`, `LSB`, `CW`, `FM`, `AM`, `DIGI`).
5. **PTT tag** — owner-drawn rounded rect, 1 px border, 2×7 px padding, 7.5 pt caps text.
   `"OFFLINE"` (`FAINT`) / `"RECEIVE"` (`FAINT`) / `"TRANSMIT"` (`ACCENT_TEXT`, and blink the
   border + text between 100 % and 40 % alpha on a 1.1 s `wx.Timer` while TX is asserted).
6. `--- vertical hairline divider, 20 px tall ---`
7. **Pause sync** — toggle button (see §7). Label `"Pause sync"` → `"Sync paused · ±NNN Hz"`
   when active, where the delta is `rig_hz - sdr_hz`. Latching: stays paused until pressed
   again. While paused the app stops pushing frequency to the WebSDR but keeps polling the rig.
8. **Mute on TX** — toggle button, label constant `"Mute on TX"`. Default **on**.
9. `--- vertical hairline divider ---`
10. **Site choice** — `wx.Choice`, `wx.EXPAND`, proportion 1, `SetMinSize((130, -1))`.
    Populated from the saved-sites store. Selecting an entry loads that site's URL and
    connects if not already connected.
11. **Connect / Disconnect** — small button. Label `"Connect"` with `ACCENT` border and
    `ACCENT_TEXT` text when the WebSDR is down; `"Disconnect"` with `BORDER`/`TEXT` when up.
12. **Undock** — 28×26 flat icon button, Lucide `external-link` glyph at 15 px, colour `MUTED`,
    hovering fills `ACCENT_TINT` and tints the glyph `ACCENT_TEXT`. Tooltip
    `"Pop receiver out to its own window"`.

**Width discipline (important).** Items 1–9 and 11–12 are fixed-width (`proportion=0`); the
site `wx.Choice` is the only stretchy child. This is what keeps the row from clipping when the
pause label grows. Do not add another fixed-width control to this row without removing one.

---

## 4. SectionBar (height 30)

Background `PANEL_ALT`, horizontal sizer, 16 px padding, 26 px between items. Three flat
toggle buttons: **TRANSCEIVER**, **SITES**, **BEHAVIOUR** — label font, ALL CAPS, no border,
colour `MUTED`, `ACCENT_TEXT` when its panel is open, `ACCENT_TEXT` on hover. No summary text
after the labels — the status bar already carries the state.

Right-aligned hint text, label font, `FAINT`: `"click again to fold"` when a panel is open,
`"folded"` when connected and closed, `"set up the rig to begin"` when the rig is down.

Clicking a button shows its panel in `SettingsHost` and hides the other two; clicking the open
one hides it. Exactly zero or one panel is ever visible. After every change call
`self.Layout()` on the frame — the receiver resizes to absorb the difference.

---

## 5. SettingsHost panels

All three are `wx.Panel` children of `SettingsHost`, created eagerly, `Hide()`n. Padding
18 px top / 16 px sides / 20 px bottom. Labels above fields, 5 px gap. Field height 26 px.

### 5.1 Transceiver
`wx.FlexGridSizer(rows=1, cols=6)`, `AddGrowableCol` on 0–4:

| Field | Widget | Default |
| --- | --- | --- |
| Backend | `wx.Choice` — rigctld / Hamlib direct / Omni-Rig / FlexRadio SmartSDR | rigctld |
| Host | `wx.TextCtrl` | `127.0.0.1` |
| Port | `wx.SpinCtrl` 1–65535 | `4532` |
| Poll interval (s) | `wx.SpinCtrlDouble` 0.05–5.0, inc 0.05, 2 dp | `0.20` |
| Reverse-sync range (Hz) | two `wx.TextCtrl` separated by the word `to` | empty (`−500` / `+500` as hints) |
| — | `[Test]` + `[Connect]`/`[Disconnect]` buttons | — |

Second row: checkbox **Use mock rig (embedded, for testing)** and, right of it, status text in
label font/`FAINT`: `"polling every 0.20 s — frequency, mode, PTT"` or
`"rigctld not reachable until connected"`.

### 5.2 Sites
Two columns, 40 px gutter.

*Left — Saved sites*: a `wx.ScrolledWindow` list, one row per site, hairline between rows.
Each row: name (value font) over URL (7.5 pt, `FAINT`, ellipsised), and right-aligned flat
buttons `Load` / `Edit` / `Delete`. `Load` sets the site, loads the URL, connects, and folds
the panel.

*Right — Add a site*: `Name` text field; `URL` text field with a `[Detect]` button beside it
(probes the URL for a WebSDR/KiwiSDR endpoint and fills in name + band); `[Save to list]` and
`[Test]`.

Persist sites as JSON next to the config file:
`{"sites": [{"name": str, "url": str, "kind": "websdr"|"kiwisdr"}], "last": str}`.

### 5.3 Behaviour
`wx.FlexGridSizer(cols=4)`, all columns growable:

| Field | Widget | Default |
| --- | --- | --- |
| CW offset (Hz) | `wx.SpinCtrl` −2000…2000 | `0` |
| Idle disconnect (min) | `wx.SpinCtrl` 0–600 (0 = never) | `60` |
| Audio device | `wx.Choice` from the system device list | System default |
| Sync direction | `wx.Choice` — `Rig → WebSDR` / `Rig ↔ WebSDR (reverse within range)` | Rig → WebSDR |

Then three checkboxes spanning two columns each:
- **Sync TX VFO (follow split transmit frequency)** — default **on**. When off, the TX VFO
  readout in the strip dims and its label becomes `"TX VFO (not synced)"`.
- **Hide receiver window when undocked (audio still plays)** — default off.
- **Keep compact bar always on top** — default on.

---

## 6. ReceiverHost

Two mutually exclusive children in a `wx.BoxSizer`, only one shown:

**6.1 Live** — `wx.html2.WebView.New(parent)` filling the host, `proportion=1, wx.EXPAND`.
Above it a 26 px header strip: left `"EMBEDDED RECEIVER · {site name}"`, right the round-trip
latency (`"142 ms"`), both label font / `FAINT`, hairline below. On Linux prefer the WebKit
backend (`wx.html2.WebViewBackendWebKit`); on Windows force Edge/Chromium
(`wx.html2.WebViewBackendEdge`) — the IE backend cannot run WebSDR's audio.

Frequency is pushed to the page with `RunScript` on the site adapter's tuning hook; poll the
page back for its current frequency when *Sync direction* is bidirectional.

**6.2 Idle** — centred column, max 46 characters wide:
kicker (label font) → heading (Cormorant 26 pt) → body paragraph (`MUTED`) → two buttons.

| Rig | Kicker | Heading | Body | Buttons |
| --- | --- | --- | --- | --- |
| down | STEP ONE | Connect the transceiver | "SDRSync polls rigctld for frequency, mode and PTT, then steers the receiver to match. Once the rig is up this panel steps aside and the receiver takes the window." | `[Connect rig]` `[Transceiver settings]` |
| up, no SDR | RECEIVER | Choose a WebSDR | "Pick a saved site from the strip above, or paste a URL. The receiver docks here and follows the rig." | `[Connect WebSDR]` `[Transceiver settings]` |

---

## 7. Controls — owner-drawn

wxPython's native buttons/checkboxes cannot be themed to this design on Windows. Implement
three small owner-drawn widgets (subclass `wx.Control`, `wx.BufferedPaintDC`, honour
`EVT_ENTER_WINDOW` / `EVT_LEAVE_WINDOW` / `EVT_LEFT_DOWN` / `EVT_SET_FOCUS`):

**FlatButton** — 1 px `BORDER` outline on transparent, radius 4, padding 5×11, value font.
Hover: fill `ACCENT_TINT`, border `ACCENT`. Pressed: fill 16 % accent.
Focus: 2 px `ACCENT` ring, 2 px offset. Disabled: 45 % opacity.
*Primary variant*: border `ACCENT`, text `ACCENT_TEXT` — **outline only, never filled.**

**ToggleButton** — same, plus a latched state drawn as the primary variant.

**CheckBox** — 14 px square, 1 px `BORDER`, radius 2. Checked: fill `ACCENT_TINT`, gold
check glyph. Label 10 pt Lora, 7 px gap, whole row clickable.

Keyboard: every control focusable, Space/Enter activates, focus ring always drawn.

---

## 8. StatusBarPanel (height 28)

Background `PANEL_ALT`, hairline on top. Left: 5 px rig dot (same rules as the strip dot) then
one line, 8 pt value font, `MUTED`:

```
{rig}  ·  {sync}  ·  {site}
rig   : "rigctld connected — 127.0.0.1:4532" | "mock rig connected" | "rig not connected"
sync  : "syncing" | "sync paused — rig 14.074.560 / sdr 14.074.500"
site  : the connected site's name, omitted when no WebSDR is connected
```

Right: flat text button **Open log folder** → `wx.LaunchDefaultApplication(log_dir)`.

---

## 9. Compact bar (undocked)

Pressing the undock icon hides `MainFrame` and shows `CompactFrame`:

```
wx.Frame(style=wx.FRAME_NO_TASKBAR | wx.CAPTION | wx.CLOSE_BOX | wx.STAY_ON_TOP)
size 720 × 92   (STAY_ON_TOP only while "Keep compact bar always on top" is checked)
```

One horizontal row, 12×16 padding: rig dot · frequency in Cormorant 22 pt (kHz group) with the
Hz remainder at 13 pt in `MUTED` · hairline · MODE group · PTT tag · stretch · `[Pause sync]` ·
`[Mute on TX]` · `[Dock]` (primary). `Dock` destroys the compact frame and re-shows `MainFrame`
with its WebView intact — reparent the WebView, do not recreate it (reloading loses the audio
session).

If *Hide receiver window when undocked* is unchecked, the receiver also gets its own plain
`wx.Frame` with the WebView reparented into it; if checked, the WebView stays alive in a hidden
frame so audio keeps playing.

---

## 10. State model

```python
@dataclass
class AppState:
    rig_connected: bool = False
    sdr_connected: bool = False
    undocked: bool = False
    open_panel: str | None = None        # "transceiver" | "sites" | "behaviour" | None
    rx_hz: int = 0
    tx_hz: int = 0
    sdr_hz: int = 0
    mode: str = "USB"
    ptt: bool = False
    paused: bool = False                 # latching
    mute_on_tx: bool = True
    sync_tx_vfo: bool = True
    mock_rig: bool = False
    site: str = ""
```

Rules:
- The rig poller runs on a worker thread; it posts to the UI with `wx.CallAfter` only when a
  value actually changed. Never touch widgets from the poll thread.
- `paused` blocks the rig→SDR push but not the rig poll, so the strip keeps showing live RX/TX
  and the status bar shows both frequencies and the delta.
- `ptt` rising edge → if `mute_on_tx`, mute the WebSDR audio; falling edge → restore the
  previous volume (store it, do not assume 100 %).
- `sync_tx_vfo` off → only `rx_hz` is pushed while transmitting on a split frequency.
- CW offset is applied to the pushed frequency only when `mode` starts with `CW`.
- Idle disconnect: if no rig frequency change and no user input for N minutes, disconnect the
  WebSDR and show the idle panel with the "Choose a WebSDR" copy.

---

## 11. Formatting helpers

```python
def fmt_hz(hz: int) -> str:          # 14074500 -> "14.074.500"
    mhz, rest = divmod(abs(hz), 1_000_000)
    khz, hzr = divmod(rest, 1_000)
    return f"{mhz}.{khz:03d}.{hzr:03d}"

def fmt_delta(a: int, b: int) -> str:  # -> "+60 Hz" / "−140 Hz"
    d = a - b
    return f"{'+' if d >= 0 else '−'}{abs(d)} Hz"
```

---

## 12. Do / don't

**Do** keep every horizontal band thin (46 / 30 / 28) so the receiver keeps the window; draw
structure with 1 px hairlines; use the accent as border and text only; give the receiver
`proportion=1` so maximising the window feeds the WebSDR, not the chrome.

**Don't** add a menu bar or toolbar (the strip *is* the toolbar); don't use native
`wx.StaticBox`/`wx.Notebook` framing; don't fill any surface with the accent; don't bold
anything; don't put the URL bar on the main screen — it lives in the Sites panel.
