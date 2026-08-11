namespace SDRSync.WebSdr;

/// <summary>
/// Shape-compatible with what a driver's OnConsole handler expects from
/// Playwright's ConsoleMessage in the Python original (.type/.text) --
/// ported from browser_shim.py's ConsoleMessage.
/// </summary>
public sealed record ConsoleMessage(string Type, string Text);

/// <summary>
/// The exact Playwright Page API surface the four WebSDR drivers use,
/// ported from sdrsync/websdr/browser_shim.py's PageLike Protocol plus the
/// WxPageAdapter concrete class's public API -- unlike the Python original
/// (which kept PageLike deliberately narrow since drivers only needed a
/// handful of Playwright-shaped calls), this interface is the FULL
/// contract a driver needs, including navigation and the trusted click,
/// since C# has no equivalent of Python's Protocol duck-typing a stub
/// class into satisfying part of an interface implicitly -- a stub used in
/// tests must implement every member explicitly regardless.
///
/// Console/pageerror delivery is push-based here (OnConsole/OnPageError),
/// matching Python's page.on("console"/"pageerror", handler) shape exactly
/// -- the concrete Avalonia adapter satisfies this via internal polling of
/// a page-side log array, not a native event bridge (see
/// AvaloniaWebSdrPage's doc comment and PORT_BRIEF.md step 5 for why:
/// Avalonia.Controls.WebView has no AddUserScript-equivalent, and the one
/// bridge that does work -- window.chrome.webview.postMessage -- is
/// WebView2-specific, not portable to the WebKitGTK backend WSL Debian
/// will use). Driver code itself is unaffected either way, since it only
/// ever registers a handler and never inspects how messages arrive.
/// </summary>
public interface IWebSdrPage
{
    /// <summary>timeoutS is a plain number of seconds (not Playwright's millisecond convention -- every call site in this port already works in seconds, via MonotonicClock/TimeSpan).</summary>
    Task NavigateAsync(string url, double timeoutS);

    /// <summary>
    /// js must be a function source ("() => ..." / "(x) => ..."), matching
    /// every real driver call site. Returns the function's return value
    /// (null if it returned undefined/null), or throws BrowserException if
    /// the call transport-failed OR the script itself threw -- ports
    /// Playwright's page.evaluate() raising PlaywrightError for either
    /// case, unlike the underlying WebView2 API this wraps (which silently
    /// swallows a JS throw and returns null with zero diagnostic
    /// information -- confirmed live, see PORT_BRIEF.md step 5). The
    /// concrete adapter is responsible for distinguishing the two by
    /// wrapping the caller's script in its own try/catch envelope.
    /// </summary>
    Task<System.Text.Json.JsonElement?> EvaluateAsync(string js, params object?[] args);

    Task WaitForFunctionAsync(string js, double timeoutS);

    /// <summary>Real OS-level trusted click at page-relative coordinates (x, y) -- see WxPageAdapter._simulate_click's doc comment for why a JS-dispatched click cannot satisfy a WebSDR's audio autoplay gate.</summary>
    Task ClickAsync(int x, int y);

    void OnConsole(Action<ConsoleMessage> handler);

    void OnPageError(Action<Exception> handler);

    /// <summary>
    /// Rebinds the callback fired when this page becomes permanently dead
    /// (a script timeout or other unrecoverable failure). Used only when
    /// switching sites on an already-open page (SDRSync.Sync's
    /// SwitchWebsdrAsync), since CreatePageAsync's own onDead parameter
    /// normally covers this.
    /// </summary>
    void SetOnDead(Action<string> onDead);

    /// <summary>
    /// Marks this page dead (does NOT fire the on-dead callback -- an
    /// explicit, caller-initiated close is not the same signal as an
    /// unexpected death). Does NOT destroy the underlying WebView widget --
    /// IWebViewHost.DestroyPageAsync owns and destroys that, same split as
    /// WxPageAdapter.close() vs. WebViewHost.destroy_page() in the Python
    /// original.
    /// </summary>
    Task CloseAsync();
}

/// <summary>
/// The narrow interface the sync engine needs from whatever manages the
/// actual browser widget, ported from sync/engine.py's local WebViewHost
/// Protocol. Deliberately does not reference Avalonia here, so the engine
/// (and tests against it) never need a real GUI application -- the
/// concrete implementation lives in SDRSync.Gui.
///
/// Unlike the Python original's create_page(loop, on_dead), this has no
/// explicit event-loop parameter: asyncio needs one to marshal a GUI-thread
/// callback onto the engine's own loop, but a Task can be awaited from any
/// thread and its continuations resume correctly without an explicit
/// handle -- the concrete implementation is responsible for any
/// GUI-thread affinity it needs internally (e.g. via a Dispatcher), not
/// this interface.
/// </summary>
public interface IWebViewHost
{
    Task<IWebSdrPage> CreatePageAsync(Action<string>? onDead = null);

    Task DestroyPageAsync(IWebSdrPage page);
}

/// <summary>
/// A live connection to one WebSDR instance, driving a real browser tab.
/// Ported from sdrsync/websdr/base.py's WebSDRDriver Protocol. Concrete
/// per-site drivers (kiwisdr/openwebrx/websdr_org/ubersdr) land in the
/// WebSDR/browser layer step -- this interface is the seam the sync engine
/// is built against.
/// </summary>
public interface IWebSDRDriver
{
    /// <summary>
    /// True once AttachAsync() has fully succeeded and stayed healthy
    /// since. TuneHzAsync/SetModeAsync/SetMutedAsync/GetStatusAsync must
    /// all be safe no-ops while this is false.
    /// </summary>
    bool Attached { get; }

    /// <summary>
    /// Given a freshly-navigated page, verify it's a compatible WebSDR
    /// instance (throw WebSDRIncompatibleException if not) and prepare it
    /// for control (e.g. satisfy the audio autoplay gate).
    /// </summary>
    Task AttachAsync(IWebSdrPage page);

    /// <summary>
    /// Set receive frequency, switching band first if necessary. Returns
    /// true only if actually applied to the page -- callers must gate any
    /// "last sent" bookkeeping on this, not assume success.
    ///
    /// verify=false tells a driver that does its own delayed/background
    /// readback-and-correct to skip arming it for this call -- used by the
    /// sync engine's periodic full resync, which re-pushes an
    /// already-unchanged frequency purely as a safety net.
    /// </summary>
    Task<bool> TuneHzAsync(int freqHz, bool verify = true);

    /// <summary>Set demodulation mode, mapping from a hamlib mode name. Returns true only if actually applied.</summary>
    Task<bool> SetModeAsync(string hamlibMode, int? passbandHz);

    Task SetMutedAsync(bool muted);

    /// <summary>Current status for display in the GUI, and the source data for reverse sync.</summary>
    Task<WebSDRStatus> GetStatusAsync();

    /// <summary>
    /// Reverse-direction (WebSDR -> rig): map this status snapshot's
    /// already-normalized mode string to a canonical hamlib mode name.
    /// Null if unmapped -- caller should skip the reverse push and log,
    /// not throw. Pure/sync -- status is expected to be a value already
    /// returned by GetStatusAsync() in the same tick.
    /// </summary>
    string? HamlibModeFromStatus(WebSDRStatus status);

    /// <summary>
    /// Reverse-direction (WebSDR -> rig): this status snapshot's
    /// frequency, converted to rig-native Hz. Null if the status has no
    /// usable frequency.
    /// </summary>
    int? RigFreqFromStatus(WebSDRStatus status);

    Task CloseAsync();
}

/// <summary>Constructs a driver for a given WebSDR site URL, ported from each driver class's Python constructor.</summary>
public delegate IWebSDRDriver WebSDRDriverFactory(string url, int cwOffsetHz);

/// <summary>
/// Ported from sdrsync/websdr/registry.py's DRIVERS dict. Empty until the
/// WebSDR/browser layer step lands the concrete drivers
/// (kiwisdr/openwebrx/websdr_org/ubersdr) -- deliberately not stubbed with
/// placeholder entries, since a driver_type with no real implementation
/// behind it should surface as "No WebSDR driver registered", exactly the
/// same path a genuinely unregistered type takes.
/// </summary>
public static class WebSDRDriverRegistry
{
    public static readonly Dictionary<string, WebSDRDriverFactory> Drivers = new();
}
