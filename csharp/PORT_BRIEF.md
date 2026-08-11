# SDRSync — C# port brief

_Last updated: 2026-08-11 (step 5 DONE: WebSDR/browser layer complete -- Avalonia WebView
page adapter, all 4 concrete drivers, fingerprint registry, trusted-click P/Invoke; 361 tests
total passing across the whole solution -- see step 5 below for exact status)_

Running log for the Python → C#/.NET port, kept separate from the main
`project_brief.md` (that file covers the Python app's own history). Steps
below match the build sequence and task numbers (#17–24) in the approved
plan: `C:\Users\ABEL75\.claude\plans\smooth-wishing-kay.md`. Update this
file at the end of every step: status, what was actually built, and any
issue hit along the way (even ones already fixed) — this is the "where are
we and what went wrong" record, not a design doc.

**Target platforms**: Windows and WSL Debian (the two platforms the Python
original already fully supports). **.NET 8** LTS, **Avalonia UI 11** for
the GUI, no third-party rig-control/XML-RPC libraries (hand-rolled —
see plan for rationale). Branch: `csharp-port`.

## Step 1 — Scaffold — DONE

`csharp/SDRSync.sln` with 6 projects: `SDRSync.Core`, `SDRSync.Rig`,
`SDRSync.WebSdr`, `SDRSync.Sync` (class libs), `SDRSync.Gui` (Avalonia
app), `SDRSync.Tests` (xUnit). All net8.0. Project references form the
dependency graph in the plan (Rig/WebSdr → Core; Sync → Core+Rig+WebSdr;
Gui → all; Tests → all).

**Issue hit**: the `dotnet new avalonia.app` template ignores
`--framework net8.0` (that flag isn't a template parameter it recognizes)
and defaulted to `net10.0` since the .NET 10 SDK is also installed
alongside 8. Fixed by editing `SDRSync.Gui.csproj`'s `<TargetFramework>`
directly after scaffolding.

NuGet packages added: `Avalonia`/`Avalonia.Desktop`/`Avalonia.Themes.Fluent`
(template defaults, 12.1.0), `Avalonia.Controls.WebView` 12.0.1 (confirmed
compatible, requires Avalonia ≥12.0.0), `Serilog`/`Serilog.Sinks.File`/
`Serilog.Extensions.Logging` on the Gui project, `Microsoft.Extensions.
Logging.Abstractions` on Core (Core depends only on the logging
abstraction, never a concrete implementation — Gui's composition root
wires the real Serilog-backed logger in at startup).

Verified: `dotnet build` and `dotnet test` clean (0 warnings/errors) on
Windows.

## Step 2 — Core (AppSettings/AppState/Format/GuiMessage) — DONE

Ported from `sdrsync/config.py`, `gui/state.py`, `gui/format.py`,
`gui_messages.py` (read directly, not from memory/summary, to guarantee
field-for-field fidelity):

- `AppSettings` — every field, every default, and the validation
  asymmetry preserved explicitly: `ClampReverseSyncBounds` swaps an
  inverted range rather than dropping it (loosening is the unsafe
  direction there), `ClampIdleDisconnectMin` fails toward `null`/off
  (over-eager disconnecting is the unsafe direction there) — same opposite
  philosophies as the Python original, not simplified away.
- `Load()`/`Save()` — hand-parses `JsonElement` per field (mirroring
  `config.py`'s per-field "wrong type → fall back to default, don't crash"
  behavior) rather than a single `JsonSerializer.Deserialize` call, since
  the latter throws on the whole payload for one bad field instead of
  degrading gracefully field-by-field.
- **Deliberate divergence from the Python original**: `Load()`/`Save()`
  take the config file path as a parameter instead of reading module-level
  globals (`CONFIG_DIR`/`CONFIG_FILE`) that tests monkeypatch. Avoids
  shared mutable static state, which would be unsafe if xUnit ever runs
  test classes in parallel. Also uses a distinct filename
  (`config-dotnet.json`, not `config.json`) so a side-by-side Python
  install's config is never read or clobbered.
- `AppState`, `Format` (FmtHz/FmtDelta/FmtHzSplit), `GuiMessage` abstract
  record base, `SiteEntry`/`KnownSites`.

Tests: 59 xUnit tests in `SDRSync.Tests`, ported directly from
`tests/test_config.py` (45 tests) and `tests/test_gui_format.py` (11
tests) — one Python test that loops over 4 bad-JSON-top-level cases became
an xUnit `[Theory]`/`InlineData` with 4 cases, hence 59 vs. 56.

**Issue hit (real bug, caught by the ported tests, not by inspection)**:
`SiteEntry` had no `JsonPropertyName` attributes, so `AppSettings.Save()`
serialized it with .NET's default PascalCase keys (`"DriverType"`) while
the hand-rolled `Load()` parser looked for the Python-style snake_case key
(`"driver_type"`) — every saved site silently vanished on the next load.
`SaveThenLoad_RoundTripsValidData` and
`SaveThenLoad_RoundTripsImportedAndCuratedSites` both failed and caught
it immediately. Fixed by adding `[property: JsonPropertyName("...")]` to
`SiteEntry`'s record parameters.

Verified: `dotnet build` 0 warnings/errors, `dotnet test` 59/59 passing.

## Step 3 — Rig layer (FlrigClient, RigctldClient, fakes) — DONE

Ported from `sdrsync/rig/base.py`, `rigctld.py`, `flrig.py`,
`fake_rigctld.py`, `fake_flrig.py` (read directly, including their long
docstrings explaining *why* each timing/reconnect decision exists — not
just the code).

- `RigState` — shared status record (SDRSync.Rig, not Core — mirrors the
  Python original's own module placement).
- `RigctldClient` — raw TCP line protocol via `TcpClient`/`NetworkStream`.
  Carried over the two hazards the plan flagged as most important to get
  right, verbatim:
  - **Stale-buffered-reply hazard**: a verify-loop readback poll that
    times out closes and immediately reconnects, never leaves the
    connection open — a cancelled read doesn't discard bytes already
    delivered into the OS socket buffer, so a late reply landing after
    timeout would otherwise get misattributed to the next command sent.
  - **`GetModeAsync()`'s RPRT-error short-circuit**: an RPRT-error reply to
    `m` is a normal response shape (rigctld's error form), not a
    transport failure — must not attempt the second readline (would block
    until the command timeout) and must not close the connection.
  - Each of `GetModeAsync()`'s two readlines gets its own full
    `cmdTimeoutS` budget (two separate `CancellationTokenSource`s), not a
    shared deadline — matches the Python original's two separate
    `asyncio.wait_for()` calls exactly.
- `FlrigClient` — hand-rolled XML-RPC over `HttpClient` (see
  `XmlRpcCodec` below). **Deliberate simplification vs. Python**: the
  Python original had to dispatch every call through
  `loop.run_in_executor()` because `xmlrpc.client` is synchronous, plus a
  dedicated `TimeoutTransport` subclass and a whole regression test
  proving the executor thread doesn't leak past `asyncio.run()`'s
  shutdown. `HttpClient` is natively async, so none of that machinery is
  needed — a plain `CancellationTokenSource` per call gets the same
  bounded-latency guarantee directly. The equivalent test
  (`ConnectAsync_BoundsAHungConnection`) is kept, but its doc comment
  explains it's now confirming the timeout bound only, not guarding
  against a hazard that no longer exists in this port.
- `XmlRpcCodec` — new file, not a port of anything specific in the Python
  source (Python's `xmlrpc.client`/`xmlrpc.server` are stdlib). Hand-rolled
  XML-RPC request/response encode+decode over `System.Xml.Linq`, covering
  only the value types flrig's 6 RPC methods actually use (string, int,
  boolean, array) — deliberately not a general-purpose XML-RPC
  implementation. Per the plan's "no 3rd-party XML-RPC library" decision:
  the NuGet options found (`Kveer.XmlRPC`, `Horizon.XmlRpc`, `xmlrpcnet`)
  are low-traffic community ports: implementing the narrow slice actually
  needed is more precise than adopting one of those as a dependency.
- `FakeRigctldServer` — `TcpListener`-based, one accept-loop task per
  server, one handler task per connected client.
- `FakeFlrigServer` — hand-rolled minimal HTTP/1.1 server over
  `TcpListener` (not `System.Net.HttpListener`, to avoid its
  platform-specific setup quirks like Windows URL ACL reservations, for
  what only ever needs to be a tiny loopback test server) plus
  `XmlRpcCodec` for the request/response bodies.
- **Deliberate divergence from Python, required not optional**:
  `FakeFlrigState`'s Python original relies on CPython's GIL to make
  individual attribute get/set atomic across its background XML-RPC
  server thread and the engine's event-loop thread. .NET has no GIL
  equivalent, so `FakeFlrigState` uses an explicit `lock` around every
  field read and write instead — flagged in the plan as a required
  change, not a stylistic one.

Tests: 60 xUnit tests across `RigctldParsingTests`/`RigctldClientTests`/
`FlrigParsingTests`/`FlrigClientTests`, ported directly from
`test_rigctld_parsing.py`, `test_rigctld_client.py`, `test_flrig_parsing.py`,
`test_flrig_client.py` (including the raw, non-fake-server stub servers
those Python tests build for timeout/stale-reply regression coverage that
the always-promptly-answering fake servers can't reproduce).

**Issue hit (real bug, caught by a ported test, not by inspection)**: my
first version of the raw stub TCP servers in `RigctldClientTests`
(`StartRawServerAsync`/`StartDelayedFreqReplyServerAsync`, the C# analog
of the Python tests' `_start_raw_server`/`_start_delayed_freq_reply_server`
helpers) called `AcceptTcpClientAsync()` exactly once instead of in a
loop. Python's `asyncio.start_server()` accepts every new connection
automatically and spawns a fresh handler per connection; my first draft
didn't replicate that, so when `RigctldClient` reconnected mid-test (after
a verify-poll timeout, exactly the scenario `SetFreq_VerifyPollTimeoutReconnectsWithoutCorruptingTheStream`
exists to exercise), nothing was listening for the second connection and
the test failed with a null readback instead of the expected confirmed
mode. This was a bug in the *test helper*, not in `RigctldClient` itself,
but it's exactly the kind of thing that would have silently made the most
important regression test in this whole layer pass for the wrong reason
(or fail confusingly) if not caught. Fixed by wrapping the accept in a
loop that spawns a handler task per connection, matching
`asyncio.start_server`'s actual behavior.

Verified: `dotnet build` 0 warnings/errors across all 6 projects,
`dotnet test` 114/114 passing (up from 59 after step 2 — the extra 55 are
this step's ported rig tests).

Standalone REPL entry points (`python -m sdrsync.rig.fake_rigctld` /
`fake_flrig`, manual dev-testing consoles) were intentionally **not**
ported — they're a manual convenience tool, not exercised by any test or
by the engine, and out of scope for this step. Flagging explicitly rather
than letting the omission go unmentioned.

## Step 4 — Sync engine — DONE

`sync/engine.py` is 2107 lines with the densest reasoning-per-line of anything ported so
far (every timing constant has a multi-paragraph "why" comment, several documenting a real
bug found during the Python original's own development). Read in full before writing any
C#, not summarized from memory.

**A real sequencing problem found while starting this step**: the engine's tick loop
depends on WebSDR abstractions (`WebSDRStatus`, a driver interface, a driver registry,
a page/browser-host interface) that don't exist yet in C# -- those are step 5's job in the
plan. Python doesn't hit this because the concrete driver modules already exist as separate,
independently-built files; the C# port is starting from nothing. Resolved by building the
*interface seams* now (`SDRSync.WebSdr`: `WebSDRStatus`, `WebSDRIncompatibleException`,
`BrowserException`, `IWebSDRDriver`, `IWebSdrPage`, `IWebViewHost`, an empty
`WebSDRDriverRegistry`; `SDRSync.Rig`: `IRigClient`, `IMockRigServer`, `IMockRigState`
retrofitted onto the already-built rig clients/fakes) and deferring the *concrete* drivers
(kiwisdr/openwebrx/websdr_org/ubersdr) to step 5 as planned. Documented here so it's clear
this wasn't scope creep into step 5 -- just the minimum seam the engine's own code needs to
compile against.

**Threading model, the other real design decision this step required**: Python's engine
leans heavily on asyncio's single-threaded event loop for correctness -- e.g. every
"generation counter" (`_forward_latch_generation`, `_sync_latch_generation`,
`_websdr_generation`) is checked immediately after an `await` and relies on nothing else
having truly run in parallel in between, only cooperatively interleaved. Plain C#
`Task`/`async`-`await` does NOT give that guarantee (continuations can resume on arbitrary
thread-pool threads). Considered `Nito.AsyncEx.Context` (the well-known NuGet package for
"single-threaded async context," exactly this need) but its last release was 2021-09 --
given the standing "avoid stale dependencies" instruction, and that the pattern itself is a
small, well-documented, ~100-line .NET idiom (a custom `SynchronizationContext` plus a
dedicated pump thread -- the same mechanism WPF/WinForms use for UI-thread affinity),
hand-rolled it instead as `SingleThreadedLoop` (`SDRSync.Sync/SingleThreadedLoop.cs`). The
whole engine runs on one dedicated instance of this; the attach supervisor is launched via
`SingleThreadedLoop.PostAndTrack()` (returns an awaitable/cancellable `Task`, the analog of
`asyncio.ensure_future()`'s returned `Task`) so it interleaves with the main tick loop
exactly like the Python original's two concurrent-but-cooperative tasks, never truly
parallel.

Ported in full, 1:1, preserving every named constant and the generation-counter guards:
`SyncEngine` (`SDRSync.Sync/SyncEngine.cs`, ~1250 lines), `StatusSnapshot`,
`ForwardPushBackoff`, `ReversePush`. `MonotonicClock` is new supporting infrastructure (no
direct Python equivalent needed, since `time.monotonic()` is stdlib) providing the
`time.monotonic()` analog.

**Deliberate divergence, C#'s nominal typing vs. Python's structural typing**: Python's
`RigClient`/`WebViewHost` are `Protocol`s defined locally inside `engine.py` and satisfied
structurally with no declaration needed on the implementing class (`RigctldClient`/
`FlrigClient` never reference `engine.py` at all). C# interfaces are nominal --
`RigctldClient : IRigClient` needs an explicit declaration, which would create a
dependency from `SDRSync.Rig` back to `SDRSync.Sync` if `IRigClient` lived next to
`SyncEngine` the way its Python counterpart does. Resolved by placing `IRigClient` in
`SDRSync.Rig` itself (next to the two concrete clients) instead, and `IWebSDRDriver`/
`IWebSdrPage`/`IWebViewHost` in `SDRSync.WebSdr` -- `SDRSync.Sync` already references both
projects, so this keeps the dependency graph a strict DAG. Noted explicitly since it's a
placement difference from the Python source, not an oversight.

**Test porting is now complete** -- all 7 of Python's engine test files (3594 lines total)
are ported, 224 xUnit tests passing across the whole solution (up from 114 after step 3):
- `test_engine_rig_backend.py` (backend selection constructs the right client) -- full.
- `test_engine_rig_timeout.py` (connect-deadline give-up) -- full.
- `test_engine_mute_on_tx.py` (PTT-edge mute/unmute, including the write-gap-deferred-not-
  dropped case) -- full.
- `test_engine_poll_interval.py` (tick cadence, live settings-change pickup) -- full, with
  one adaptation: Python monkeypatches `SyncEngine._tick` on the class itself to count
  calls, which has no C# equivalent against a sealed class's method. Ported instead to
  count `StatusSnapshot` messages drained from the public `StatusReader` channel -- every
  `TickAsync()` call publishes exactly one snapshot regardless of code path, so this is an
  equivalent black-box proxy for tick count, arguably a cleaner seam than monkeypatching
  since it's the one already publicly exposed for the GUI to consume.
- `test_engine_mode_independence.py` (856 lines, 25 test functions) -- full, including the
  v14 "good network citizen" write-rate-limiting suite (global write gap, per-axis failure
  ladder doubling/capping/exponent-clamping, periodic-resync stamp correctness, and the
  forward-side generation-guard races against a slow/suspended driver call). All 25 passed
  on the first run after porting -- no bugs found in this pass (contrast with steps
  1-4's earlier bugs, all caught immediately by their first test run too).
- `test_engine_reverse_sync.py` (1107 lines, 31 test functions) -- full, covering the
  WebSDR->rig direction's baseline capture, debounced freq/mode push, echo suppression
  (including a collapsed-forward-mode regression, e.g. rig mode `PKTUSB` forward-pushed but
  the page still reads back `USB`), TX suppression, `ptt=None` inheriting last-known TX
  state, out-of-band/unmapped-mode handling, the retry ladder (success-after-retry, give-up
  reverting `_lastSentFreq`/`_lastSentModeKey` to null so the engine re-asserts the rig's
  real value), rapid-mode-switch superseding a stale in-flight ladder, the cross-axis
  rig-write-rate floor, CW-variant echo (rig's own `CW-L` vs. falling back to canonical
  `CW`), Hold engage/release (including cancelling an in-flight ladder mid-`await` without
  leaving a stale give-up error behind), `ResetSyncLatches` NOT releasing Hold, and the
  reverse-sync min/max range guard (checked against the rig-*native* frequency, not the raw
  page frequency -- verified with a driver whose `RigFreqFromStatus` applies a genuine
  offset, the way a real CW-offset-converting driver would). All 31 passed on the first
  run after porting -- no bugs found in the engine code itself; see below for the one
  supporting-type change this step required.
- `test_engine_switch_site.py` (1244 lines, 42 test functions) -- full. Covers the rig/WebSDR
  independent-lifecycle rule (switching WebSDR never touches rigctld; stopping the rig DOES
  cascade-stop an active WebSDR session, the deliberate v9 asymmetry), `StartWebsdrAsync` vs.
  `SwitchWebsdrAsync` (the latter reuses the existing WebView instead of destroy+recreate --
  the fix for a real string of Win32 z-order bugs), the on-dead callback rebind after a
  switch, the v14 attach supervisor (flap-vs-genuine-drop distinction, jittered/clamped retry
  ladder including the ~1024-failures overflow-avoidance clamp, the preflight-gate cheap-check
  skip once the ladder is deep), and the v14 idle-disconnect feature in full (per-field
  None/null-safe activity tracking so a dropped CAT read can't reset OR mask the idle timer,
  auto-resume on the next rig activity, rate-limited retry on a resume that keeps failing, and
  the three "was the idle-stopped site since edited / deleted / never listed at all" resume
  paths). All 42 passed after one required infrastructure fix (below) -- no bugs found in the
  engine's own switch-site/idle-disconnect logic itself.

**Required infrastructure addition, found while porting this file**: `SyncEngine.cs` had no
seam equivalent to the Python tests' `engine._attach_supervisor = noop` / `engine._sleep_or_stop
= fake_sleep` / `monkeypatch.setattr(engine_module, "check_websdr_url", fake_check)` --
Python satisfies all three by reassigning an instance's bound method or a module-level function
reference, which C# has no equivalent of (an instance method can't be reassigned, and a static
call is not interceptable). Without a substitute, every `StartWebsdrAsync`/`SwitchWebsdrAsync`
call in a test would launch the REAL `AttachSupervisorAsync` onto `SyncEngine`'s real background
`SingleThreadedLoop` thread (started unconditionally in its constructor), looping on real
`Task.Delay` calls (`AttachCheckIntervalS` = 1s) forever, uncancelled, for the rest of the test
process's life. Added three settable delegate fields --
`_attachSupervisor`/`_sleepOrStop`/`_checkWebsdrUrl` -- defaulting to the real implementations
in the constructor, with `StartWebsdrAsync`/`SwitchWebsdrAsync`/`AttachSupervisorAsync` now
calling through them instead of the concrete methods directly. Zero behavior change for the
real (GUI) code path; this is purely the missing test seam, added because it was genuinely
required, not speculative.

**Second issue hit while porting this file (test-only, not an engine bug)**: `OnPageDead()`
early-returns if `!_started`, and `_started` is only set `true` inside `RunAsync()` -- the
C# analog of the Python tests' `engine._loop = asyncio.get_running_loop()` one-liner (which
lets `_on_page_dead()` work without actually running the full `run()` coroutine). Five ported
tests that call `engine.OnPageDead(...)` (directly, or indirectly via the switch-rebound
callback) needed `engine._started = true;` added after construction, or the call was silently
a no-op and the assertion after it happened to pass or fail for the wrong reason --
`StalePageDeathNotification_IsIgnored` in particular "passed" on the first run for a false
reason (the notification was ignored because `OnPageDead` never even fired, not because the
generation check worked) before this fix. Caught by re-reading the test's own logic during
verification, not by a failing assertion, so flagging explicitly rather than letting a
silently-vacuous test stand. Also required bumping the recreate-and-settle wait from two
`Task.Yield()`s to a real `await Task.Delay(50)`, since `HandlePageDead`'s recovery genuinely
runs on a separate OS thread here (unlike Python's single-threaded event loop, where
`await asyncio.sleep(0)` truly is enough to let a `call_soon_threadsafe`-scheduled callback
run).

**Supporting-type change required for reverse-sync tests**: `WebSDRStatus`
(`SDRSync.WebSdr/WebSDRStatus.cs`) changed from a positional record with `{ get; init; }`
properties to explicit `{ get; set; }` mutable properties on all five fields. The Python
tests mutate a single shared `status` object's fields in place between ticks
(`status.freq_hz = 14200000`) to simulate the WebSDR page's live state changing -- a
non-frozen dataclass allows this trivially, but a C# positional record's init-only
properties don't. Verified this doesn't break `SyncEngine.Publish()`'s `with` expression
usage (`effectiveWebsdr with { LastError = ... }`) -- `with` still works on a record
regardless of property mutability, confirmed by the full suite passing.

**Deliberate test-porting adjustments made while porting `test_engine_mode_independence.py`**:
- `ForwardPushBackoff.Failures`/`.NextAttemptAt` changed from `{ get; private set; }` to
  `{ get; internal set; }` -- several Python tests set `.failures`/`.next_attempt_at`
  directly to probe a specific ladder rung without looping (e.g.
  `test_forward_push_backoff_exponent_is_clamped`), mirroring Python's lack of attribute
  privacy. `LastTarget` stayed `private set` since no test needed to set it directly.
- `asyncio.Event()` (used by the Python tests' `SlowModeDriver`/`SlowTuneDriver` to let the
  test know a driver call is mid-flight before injecting a concurrent reset/Hold-toggle) has
  no single-line C# equivalent -- ported as a `TaskCompletionSource` the driver
  `TrySetResult()`s on entry and the test `await`s, with a `ResetEnteredSignal()` method
  (swaps in a fresh `TaskCompletionSource`) standing in for `asyncio.Event.clear()` in the
  one test that reuses the signal across two ticks.

**Issue hit (real bug, caught by a ported test)**: the first version of `MonotonicClock`
measured elapsed time since the class was first touched (via `Stopwatch.GetTimestamp()`
relative to a static-init-time epoch), starting near zero. Several engine fields use `0.0`
as a sentinel meaning "long ago, never held back by this write-rate gate"
(`_lastWebsdrWriteAt`, `_lastRigWriteAt`, etc.) -- this only works if "now" is reliably a
large number, which Python's `time.monotonic()` (seconds since boot) always is in practice.
With the class-load-relative clock, a test running soon after the type was first touched
saw `NowS() - 0.0` read as "not long enough ago yet," and the very first PTT-edge mute call
in `EngineMuteOnTxTests` silently failed to fire. Fixed by switching to
`Environment.TickCount64 / 1000.0` (milliseconds since OS boot) -- large in any realistic
run, matching `time.monotonic()`'s actual real-world magnitude, not just its API shape.

**Not yet wired to anything real**: no GUI, no real WebViewHost, no concrete WebSDR
drivers -- `SyncEngine` compiles and its tick loop's own logic is exercised by tests using
stub `IRigClient`/`IWebSDRDriver` implementations, same as the Python original's own engine
tests do. End-to-end wiring happens in steps 6-7.

## Step 5 — WebSDR/browser layer — DONE

Interface seams (`IWebSDRDriver`, `WebSDRStatus`, `IWebSdrPage`, `IWebViewHost`, the empty
`WebSDRDriverRegistry`) already exist (built ahead of schedule in step 4, out of necessity
-- see above). What's left for this step: the concrete `Avalonia.Controls.WebView`-backed
page adapter, the 4 concrete drivers (kiwisdr/openwebrx/websdr_org/ubersdr) with their exact
JS snippets, fingerprint-based registry population, and the trusted-click P/Invoke for the
autoplay-gesture requirement.

**Read in full before designing anything**: `sdrsync/websdr/base.py` (114 lines, the
`WebSDRDriver` Protocol), `sdrsync/websdr/registry.py` (49 lines), `sdrsync/websdr/
websdr_org.py` (484 lines, fully read -- the smallest driver, used as the reference pattern),
`sdrsync/websdr/browser_shim.py` (717 lines, fully read -- the wx.html2-backed `PageLike`
implementation this step's Avalonia adapter replaces). kiwisdr.py/openwebrx.py/ubersdr.py not
yet read in full -- next.

**Pre-implementation spike, done live on this machine before writing any driver/adapter
code** (rule #1: no guessing at an unverified native-control API contract for the piece
everything else in this step depends on). `browser_shim.py`'s own docstring explicitly says
its design was "verified live, not assumed" against wx.html2's real behavior, including two
review-driven revisions that each found real bugs by testing against the live API rather than
trusting docs -- same standard applied here. Built a throwaway Avalonia app
(`Avalonia.Controls.NativeWebView`, package already vendored in `SDRSync.Gui.csproj`,
WebView2 Runtime 151.0.4129.72 confirmed installed) in the scratchpad dir, not committed,
and drove it against a real WebView2-backed page. Findings, each of which materially shapes
the adapter design below:

1. **`NativeWebView.InvokeScript(string script)` returns `Task<string>` already
   JSON-serialized** -- `1+1` -> `"2"`, an object literal -> clean JSON (`{"a":1,"b":"x"}`),
   `undefined`/`null` -> `"null"`. Confirms the plan's expectation that this eliminates the
   Python wx shim's manual `JSON.stringify()`-wrapping-plus-parse dance entirely: no
   double-encoding needed, just `JsonDocument.Parse(result)` on whatever comes back.
2. **A thrown JS exception (ReferenceError or explicit `throw`) does NOT propagate as a C#
   exception** -- `InvokeScript("nonexistent.property.chain")` returns `"null"` silently, with
   zero information about the failure. This is the opposite of Playwright's `page.evaluate()`
   (which the Python drivers rely on raising `PlaywrightError` to detect a failed call) and is
   NOT something the plan anticipated. Worked around by having `EvaluateAsync()` wrap every
   caller script in a JS-side try/catch that returns `{ok:true, value}` or
   `{ok:false, error}` as a single object (confirmed live: `{"ok":false,"error":"nonexistent
   is not defined"}` / `{"ok":false,"error":"boom"}` for an explicit `throw new
   Error('boom')`) -- `EvaluateAsync()` throws `BrowserException(error)` when `ok` is false.
   This is a deliberate, verified adaptation, not a port of anything in browser_shim.py (which
   didn't need this pattern, since wx's `EVT_WEBVIEW_SCRIPT_RESULT.IsError()` already
   distinguished a JS throw from a success).
3. **`InvokeScript` must be called from the UI thread** -- calling it from a plain
   `Task.Run(...)` background thread throws `COMException: This method can only be called
   from the thread that created the object.` (0x802A000C). Confirmed `await
   Dispatcher.UIThread.InvokeAsync(async () => await webView.InvokeScript(...))` from a
   background thread works cleanly and returns the real result. So the adapter still needs a
   GUI-thread-marshaling step per call (the plan's claim that "a Task can be awaited from any
   thread and its continuations resume correctly without an explicit handle" is true for
   awaiting, but not for *initiating* the call) -- just via one line of `Dispatcher.UIThread
   .InvokeAsync`, not the wx shim's whole hand-rolled Future/CallAfter/per-adapter-lock
   machinery (that machinery existed because wx.html2's `EVT_WEBVIEW_SCRIPT_RESULT` carries no
   usable request ID and results must be correlated by strict FIFO ordering + a lock --
   `InvokeScript`'s own `Task<string>` already IS the correlated result, no lock needed).
4. **`NavigationCompleted` fires reliably (`IsSuccess=true`) for `about:blank` and a real
   `https://` URL**, confirmed via an actual page load (`document.title` read back correctly
   afterward) -- safe to use as the "page loaded" signal `NavigateAsync()` awaits, the same
   role `goto()` plays in every driver's `attach()`. (It did NOT fire for a `data:` URL in this
   same spike, but no driver ever navigates to a `data:` URL in production -- that was only an
   artifact of the spike's own test harness, not a production-relevant finding, flagged here
   only so a future reader doesn't misread the omission as a real gap.)
5. **No `AddUserScript`/"run on every future navigation" equivalent exists** on this API
   surface (checked the full public member list via reflection against the installed
   package) -- `browser_shim.py`'s persistent console/pageerror-forwarding shim
   (`AddUserScript`, survives every subsequent navigation automatically) has no direct
   analog. Two paths considered: (a) reinject the shim via `InvokeScript` after every
   `NavigateAsync()` call (cheap, since drivers only navigate at attach and retry, not per
   tick), using `window.chrome.webview.postMessage(...)` to push messages out --
   confirmed live that this fires `WebMessageReceived` with the exact JSON body sent, but
   `window.chrome.webview` is WebView2-specific JS (not portable to the WebKitGTK backend
   WSL Debian will actually use, and Linux has NOT been live-verified this session); (b) skip
   any push/event bridge entirely and have the reinjected shim just APPEND to a page-side
   array (`window.__sdrsyncLog`), with the adapter periodically draining it via the
   already-proven-portable `InvokeScript`/`EvaluateAsync` path (same mechanism on every
   backend, no backend-specific JS API name to get wrong). **Chose (b)** -- console/pageerror
   forwarding in the Python original is diagnostic-only (feeds `logger.warning`/
   `_last_page_error`, never branches driver control flow), so trading push-immediacy for
   cross-platform certainty is the right tradeoff, and it's the only option here that doesn't
   require Linux to actually verify. Documented as a deliberate divergence from
   `browser_shim.py`'s design, not an oversight.

**Adapter implementation** (`SDRSync.Gui`, since it's the project that already references
`Avalonia.Controls.WebView` and owns the real widget instance -- mirrors `browser_shim.py`
being GUI-layer code in the Python original too):

- `IWebSdrPage` (`SDRSync.WebSdr/IWebSDRDriver.cs`) was expanded well beyond its step-4
  placeholder shape (which had only `SetOnDead`) to the full contract a driver needs:
  `NavigateAsync`, `EvaluateAsync` (returns `JsonElement?`), `WaitForFunctionAsync`,
  `ClickAsync`, `OnConsole`/`OnPageError` (push-style handler registration, kept
  Playwright-shaped for driver-code fidelity even though the concrete adapter satisfies it
  via polling -- see finding 5 above), and `CloseAsync`. Unlike `browser_shim.py`'s
  `PageLike` Protocol (deliberately narrow, since Python drivers only needed a handful of
  calls and structurally satisfied the rest), this is the FULL interface, because a C# stub
  used in tests must implement every member explicitly -- no partial structural typing.
- `AvaloniaWebSdrPage` (`SDRSync.Gui/AvaloniaWebSdrPage.cs`) -- the `WxPageAdapter` analog.
  Substantially smaller than the 717-line Python original because of what the spike proved
  unnecessary (no per-adapter FIFO lock, no CoreWebView2-readiness future, no hand-rolled
  Future/CallAfter plumbing -- `Dispatcher.UIThread.InvokeAsync(Func<Task<T>>)` does the
  GUI-thread marshaling in one line). Still ports the parts whose underlying need didn't go
  away: a `ScriptTimeoutS` (15s) watchdog around every `EvaluateAsync` call that marks the
  adapter permanently dead and fires `on_dead` at most once if a script never returns (can't
  cancel an in-flight script; the eventual real result would otherwise land on a later,
  unrelated call -- identical reasoning to the Python original, different mechanism); the
  JS-side try/catch envelope (finding 2); and the console/pageerror log-collector shim,
  reinjected after every `NavigateAsync()` (finding 5).
- `AvaloniaWebViewHost` (`SDRSync.Gui/AvaloniaWebViewHost.cs`) -- the `WebViewHost` analog,
  but deliberately scoped narrower than the 538-line Python original. Ported: `Attach(Panel
  parent)`, `CreatePageAsync`/`DestroyPageAsync` (construct/destroy a real `NativeWebView`
  child control). **Not ported here, left to steps 6/7**: `reparent()` (spec §9 popout/dock)
  and `present()`'s real win32 z-order dance (`bring_pair_to_front`/`_kick_to_top`/
  `AttachThreadInput`, ~250 lines in the Python original) -- both are genuinely
  GUI-shell-coupled (need a real `MainWindow`/`CompactWindow` pair that doesn't exist until
  step 6), and the Python original itself keeps this logic in a separate module
  (`gui/webview_host.py`) from the page adapter (`browser_shim.py`) for the same reason.
  `AvaloniaWebSdrPage` still accepts an on-screen-presenter callback (currently wired to a
  no-op `Present()` placeholder) so that wiring can land later without changing either
  class's shape -- confirmed this doesn't block anything by design, not by omission.
- `TrustedClick` (`SDRSync.Gui/TrustedClick.cs`) -- the real OS-level click needed to satisfy
  each WebSDR's audio autoplay gate (Chromium/WebKit both reject JS-dispatched clicks for
  this purpose, confirmed for wx during the Python original's own Block A spike; unchanged
  here since the requirement is the browser engine's, not the GUI toolkit's). Windows: P/Invoke
  `SendInput` (user32.dll), using `MOUSEEVENTF_ABSOLUTE` normalized against
  `GetSystemMetrics(SM_XVIRTUALSCREEN/...)` so multi-monitor setups where the virtual desktop
  doesn't start at (0,0) still land correctly -- this is documented `SendInput` behavior, not
  independently discovered. Linux: P/Invoke `XTestFakeMotionEvent`/`XTestFakeButtonEvent`
  (`libXtst.so.6`), written directly against libXtst's documented API shape but, unlike the
  Windows path, **not live-verified this session** (no WSL Debian GUI available here) --
  flagged explicitly, matching this port's standing practice for anything Linux-specific that
  hasn't been exercised yet.
- Screen-coordinate translation for `ClickAsync` uses `Avalonia.VisualExtensions
  .PointToScreen(Visual, Point)` (confirmed via reflection against the installed package --
  a direct control-local-to-screen-pixel call, the same role wx's `WebView.ClientToScreen()`
  played; no `TopLevel`/`TranslatePoint` chain needed once this was found).
- **No unit tests for the adapter/host/trusted-click classes** -- matches the Python
  original's own coverage boundary exactly: `browser_shim.py` and `gui/webview_host.py` have
  zero test files in the Python suite either (confirmed by search), because both need a real
  browser engine or real OS-level window to test meaningfully, and were "verified live, not
  assumed" during development instead (the spike above is this port's equivalent exercise).
  Not a coverage gap being silently accepted -- a deliberate, precedented boundary.

**The four concrete drivers** (`SDRSync.WebSdr/{WebsdrOrgDriver,KiwiSDRDriver,
OpenWebRXDriver,UberSDRDriver}.cs`), each read from its Python source in full before porting,
each driving its site's real control JS/API exactly as the original does (every JS snippet
copied verbatim, only the outer C# call plumbing differs):

- `WebsdrOrgDriver` (484-line source) -- band-table-based tuning (`setband`/`setfreq`),
  N-suffix narrow-mode variants, delayed background frequency-verification task
  (`ScheduleFreqVerification`/`VerifyFreqAppliedAsync`, cancellable via
  `CancellationTokenSource` -- the C# analog of `asyncio.Task.cancel()`), rate-limited
  out-of-band/unmapped-mode warnings. 32 tests ported (band selection incl. the rate-limited
  warning regression, mode mapping, reverse mode mapping, status mode normalization, the
  verify-flag regression) -- all passed first run.
- `KiwiSDRDriver` (412-line source) -- single continuous-range receiver (no band table),
  `ext_tune()`'s "omitted arg means don't change this" calling convention, synchronous
  inline readback verification (no background task, unlike websdr_org), `"nan"` frequency
  handling right after attach. 29 tests ported -- all passed first run.
- `OpenWebRXDriver` (534-line source) -- offset-relative tuning against a mutable
  center/bandwidth SDR profile, the cached-profile out-of-range pre-check (avoids a page
  round-trip for an already-known-rejected frequency), profile-change detection forcing a
  reattach. 16 tests ported (mode mapping + reverse mapping only -- the Python original has
  no dedicated status-mode test file for this driver either) -- all passed first run, 0
  warnings.
- `UberSDRDriver` (948-line source, the largest) -- the one driver against a *documented*
  versioned control API rather than page internals: the in-page JS agent (`AgentJs`, copied
  verbatim -- real browser-side protocol JS, not something to "port" line-by-line into C#
  idioms) bridging `window.dispatchEvent`/`addEventListener` CustomEvents to
  request/response semantics; `CallAsync`'s synchronous-reply-usually-already-in-hand /
  poll-if-not pattern; `PassbandEdges()`'s sideband-aware filter-edge math; `V2PageUrl()`'s
  URL resolution (ported using `Uri`/`UriBuilder` instead of Python's
  `urlsplit`/`urlunsplit` -- confirmed byte-for-byte matching output across all 8 ported URL
  cases, including query-string preservation and the port/path-prefix-survives cases). 49
  tests ported (mode mapping + passband edges, v2 URL resolution, and the full driver
  behavior suite -- PTT duck/mute, tuning, mode+filter-in-one-command with refused-filter
  retry, status/reverse-sync, goodbye-on-close, JSON-serializability of every sent payload)
  -- all passed first run.
- **No bugs found in any of the four drivers' own logic** during porting -- every test file
  passed on its first run once written. The one genuine bug caught this step was in test
  infrastructure timing/threading (see the `_started` gate and `Task.Delay` findings from
  step 4's switch-site work, same category, none new this step).

**Registry** (`SDRSync.WebSdr/WebSdrRegistry.cs`, ported from `registry.py`'s 49 lines):
`RegisterBuiltinDrivers()` populates the (deliberately empty-until-now, see step 4)
`WebSDRDriverRegistry.Drivers` dictionary with all four driver factories; `DetectDriverType()`
guesses a `driver_type` from a site's root-page HTML via `<script src="...">` fingerprint
matching, refusing to guess (returning `null`) on zero or multiple matches. **Deliberate
divergence**: Python's `DRIVERS` dict stores each driver *class* and introspects
`driver_cls.FINGERPRINT_MARKERS` via `getattr()` at detection time; `WebSDRDriverRegistry
.Drivers` stores a factory *delegate* instead (required since step 4, so `SDRSync.Sync`
never depends on concrete driver types), which can't be introspected for its markers the
same way -- so `WebSdrRegistry` keeps a small separate `driver_type -> FingerprintMarkers`
table built from each driver's own public `FingerprintMarkers` array (the "declared
alongside its control logic" source of truth is unchanged; only how detection re-reads it
differs). `RegisterBuiltinDrivers()` is not auto-run (no static-constructor side effect) --
it needs an explicit call from the GUI composition root once one exists (step 6/7); an
explicit call reads clearer than implicit registration and keeps engine/driver unit tests
free to populate only the registry entries a given test needs, as they already do. 11 tests
ported from `test_fingerprint.py` (including the two- and three-way ambiguous-match cases,
and the "marker string only in body text, not a `<script src>`, is not a match" case) plus
one new test confirming `RegisterBuiltinDrivers()` actually wires all four types -- all passed.

**Issue hit (real bug, caught by a full-suite run, not by inspection)**: the new
`RegisterBuiltinDrivers_PopulatesAllFourDriverTypes` test writes the four REAL driver
factories into `WebSDRDriverRegistry.Drivers`, the same shared static dictionary
`EngineSwitchSiteTests` (step 4) writes stub factories into under the same keys
("websdr_org"/"kiwisdr"). xUnit runs different test classes in parallel by default (only
methods *within* one class are serialized) -- so the two classes intermittently raced,
occasionally leaving `EngineSwitchSiteTests` reading back a real `WebsdrOrgDriver` where it
expected its own `StubDriver`, failing with an `InvalidCastException` (`StopRig_
AlsoStopsAnActiveWebsdrSession`, caught on a routine re-run of the full suite -- passed
clean moments earlier, which is exactly the signature of a cross-class race, not a logic
bug). Fixed by pinning both test classes into a shared xUnit `[Collection("WebSDRDriver
Registry")]` (defined in `WebSdrTestHelpers.cs`), which serializes them relative to each
other while leaving both free to run in parallel with the rest of the suite. Confirmed fixed
by running the full suite 5 times in a row post-fix (previously reproduced within 1-2 runs).

**Verified**: `dotnet build` 0 warnings/errors across all 6 projects; `dotnet test` 361/361
passing (up from 224 after step 4 -- the extra 137 are this step's driver/registry tests: 32
+ 29 + 16 + 49 + 11 = 137). Still not wired into any real running app -- no GUI shell exists
yet (step 6), so the adapter/host/trusted-click classes are unexercised beyond the
Program.cs-less spike and compiling cleanly against the real `Avalonia.Controls.WebView`
package. WSL Debian verification has still not happened for this step (or any step) --
flagging again per this port's standing practice; the console-log-polling design (finding 5)
was specifically chosen to reduce, not eliminate, that risk when Linux verification does
eventually happen.

## Step 6 — GUI shell — NOT STARTED

## Step 7 — Feature parity pass — NOT STARTED

## Step 8 — Packaging & cross-platform verification — NOT STARTED

Neither the Windows build nor the WSL Debian build has been exercised yet
beyond `dotnet build`/`dotnet test` on Windows. WSL Debian verification
(the user's second target platform) hasn't happened for any step yet —
flag this explicitly each step until it does, don't let it go unmentioned
the way it easily could once the GUI project isn't buildable there yet
anyway (Avalonia.Controls.WebView needs WebKitGTK dev/runtime packages
installed in WSL Debian, not yet confirmed present).
