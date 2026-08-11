using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Text.Json;
using System.Text.RegularExpressions;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Threading;
using Microsoft.Extensions.Logging;
using SDRSync.Core;
using SDRSync.WebSdr;

namespace SDRSync.Gui;

/// <summary>
/// Wraps one <see cref="NativeWebView"/> child control, ported from
/// sdrsync/websdr/browser_shim.py's WxPageAdapter -- but considerably
/// smaller, because Avalonia.Controls.WebView's own API already provides
/// most of what WxPageAdapter had to hand-build against wx.html2's cruder
/// one. See PORT_BRIEF.md step 5 for the live spike this design is based
/// on (rule #1: verified live, not assumed, same standard the Python
/// original's own docstring holds itself to). Concretely, NOT ported
/// because it isn't needed here:
///   - The per-adapter serialization lock (wx.html2's script-result event
///     carries no usable request ID, so results had to be FIFO-correlated
///     by hand). InvokeScript()'s own Task&lt;string&gt; already IS the
///     correlated result, and WebView2's ExecuteScriptAsync natively
///     supports overlapping concurrent calls -- confirmed by design intent,
///     not just absence of a documented lock requirement.
///   - The "wait for CoreWebView2 to exist" readiness future (confirmed
///     live: NativeWebView answered Navigate()/InvokeScript() immediately
///     after construction with no observed race).
///   - The whole Future/CallAfter/call_soon_threadsafe plumbing --
///     Dispatcher.UIThread.InvokeAsync(Func&lt;Task&lt;T&gt;&gt;) does this
///     natively in one line (confirmed live: InvokeScript MUST originate on
///     the UI thread -- calling it from a plain background Task throws
///     COMException 0x802A000C -- but the awaited Task<T> result crosses
///     back to the caller's thread with no extra plumbing needed).
/// Still ported, because the underlying need is unchanged:
///   - Marking the adapter permanently dead after a script hangs past a
///     timeout (can't cancel an in-flight script; the eventual real result
///     would otherwise land on a later, unrelated call).
///   - The on_dead notification contract (fires at most once).
///   - A JS-side try/catch envelope around every EvaluateAsync call --
///     needed here for a DIFFERENT reason than wx's evt.IsError(): the
///     underlying WebView2 call swallows a thrown JS exception entirely
///     (confirmed live: returns "null" with zero diagnostic information),
///     so this adapter's own envelope is the only way to recover the
///     error's message at all, not just the only way to detect one.
///   - Console/pageerror forwarding -- but via periodic polling of a
///     page-side log array instead of a push bridge, since
///     Avalonia.Controls.WebView has no AddUserScript-survives-every-
///     navigation equivalent, and the one push bridge confirmed working
///     live (window.chrome.webview.postMessage) is WebView2-specific, not
///     portable to the WebKitGTK backend WSL Debian will use.
/// </summary>
public sealed class AvaloniaWebSdrPage : IWebSdrPage
{
    private const double ScriptTimeoutS = 15.0;
    private const double PollIntervalS = 0.1;
    private const double ConsoleLogPollIntervalS = 1.0;

    // Matches a function-source string ("() => ...", "(x) => ...",
    // "async (x) => ...", "function(x) {...}"), same shape as
    // browser_shim.py's _FUNC_RE -- used only by WaitForFunctionAsync,
    // which (unlike EvaluateAsync, where every real driver call site
    // always passes a function) accepts either a function or a raw
    // boolean expression.
    private static readonly Regex FuncRe = new(
        @"^\s*(async\s+)?(\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>|^\s*(async\s+)?function\b",
        RegexOptions.Compiled);

    private readonly NativeWebView _webView;

    /// <summary>The wrapped control -- exposed so AvaloniaWebViewHost can remove it from its parent panel on teardown.</summary>
    internal NativeWebView WebView => _webView;

    private readonly Action<bool>? _onScreenPresenter;
    private Action<string>? _onDead;
    private readonly List<Action<ConsoleMessage>> _consoleHandlers = new();
    private readonly List<Action<Exception>> _pageErrorHandlers = new();

    private volatile bool _alive = true;
    private bool _deadReported;
    private bool _warnedNoPresenter;
    private CancellationTokenSource? _consolePollCts;

    public AvaloniaWebSdrPage(NativeWebView webView, Action<bool>? onScreenPresenter = null, Action<string>? onDead = null)
    {
        _webView = webView;
        _onScreenPresenter = onScreenPresenter;
        _onDead = onDead;
    }

    public void OnConsole(Action<ConsoleMessage> handler) => _consoleHandlers.Add(handler);

    public void OnPageError(Action<Exception> handler) => _pageErrorHandlers.Add(handler);

    public void SetOnDead(Action<string> onDead) => _onDead = onDead;

    public async Task NavigateAsync(string url, double timeoutS)
    {
        if (!_alive) throw new BrowserException("page adapter is not attached (destroyed)");

        var navDone = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        void Handler(object? _, WebViewNavigationCompletedEventArgs e) => navDone.TrySetResult(e.IsSuccess);

        await Dispatcher.UIThread.InvokeAsync(() =>
        {
            _webView.NavigationCompleted += Handler;
            _webView.Navigate(new Uri(url));
        });

        try
        {
            var completed = await Task.WhenAny(navDone.Task, Task.Delay(TimeSpan.FromSeconds(timeoutS)));
            if (completed != navDone.Task)
            {
                throw new BrowserException($"goto('{url}') timed out after {timeoutS}s");
            }

            if (!navDone.Task.Result)
            {
                throw new BrowserException($"navigation to '{url}' failed");
            }
        }
        finally
        {
            await Dispatcher.UIThread.InvokeAsync(() => _webView.NavigationCompleted -= Handler);
        }

        // Re-inject the console/pageerror log-collector shim -- there is no
        // AddUserScript-equivalent that would survive this navigation on
        // its own (see class doc comment), so it's done fresh every time.
        await InstallConsoleShimAsync();
        StartConsolePolling();
    }

    public async Task<JsonElement?> EvaluateAsync(string js, params object?[] args)
    {
        if (!_alive) throw new BrowserException("page adapter is not attached (destroyed)");

        var argsJson = string.Join(", ", args.Select(a => JsonSerializer.Serialize(a)));
        // Envelope: the caller's function is invoked inside a JS try/catch
        // and the outcome returned as a single object -- NOT
        // JSON.stringify()'d by us, since InvokeScript already serializes
        // its own completion value (confirmed live; double-stringifying
        // would double-encode). ok:false carries whatever message the
        // thrown value has, since the underlying WebView2 call otherwise
        // swallows it completely (confirmed live).
        var script =
            $"(() => {{ try {{ const __v = ({js})({argsJson}); " +
            "return {ok:true, value: __v === undefined ? null : __v}; } " +
            "catch (e) { return {ok:false, error: String((e && e.message) || e)}; } })()";

        string? raw;
        try
        {
            var invokeTask = Dispatcher.UIThread.InvokeAsync(async () => await _webView.InvokeScript(script));
            var completed = await Task.WhenAny(invokeTask, Task.Delay(TimeSpan.FromSeconds(ScriptTimeoutS)));
            if (completed != invokeTask)
            {
                MarkDead($"script timed out after {ScriptTimeoutS}s");
                throw new BrowserException($"script timed out after {ScriptTimeoutS}s (adapter marked dead)");
            }

            raw = await invokeTask;
        }
        catch (BrowserException)
        {
            throw;
        }
        catch (Exception e)
        {
            throw new BrowserException($"InvokeScript failed: {e.Message}", e);
        }

        if (raw is null) throw new BrowserException("InvokeScript returned no result");

        using var doc = JsonDocument.Parse(raw);
        var root = doc.RootElement;
        if (!root.TryGetProperty("ok", out var okProp) || !okProp.GetBoolean())
        {
            var error = root.TryGetProperty("error", out var errProp) ? errProp.GetString() : "unknown error";
            throw new BrowserException(error ?? "unknown error");
        }

        if (!root.TryGetProperty("value", out var valueProp) || valueProp.ValueKind == JsonValueKind.Null)
        {
            return null;
        }

        return valueProp.Clone();
    }

    public async Task WaitForFunctionAsync(string js, double timeoutS)
    {
        var expr = FuncRe.IsMatch(js) ? $"({js})()" : $"({js})";
        var deadline = DateTime.UtcNow.AddSeconds(timeoutS);
        while (true)
        {
            if (!_alive) throw new BrowserException("page adapter is not attached (destroyed)");
            try
            {
                var result = await EvaluateAsync($"() => !!({expr})");
                if (result is { ValueKind: JsonValueKind.True }) return;
            }
            catch (BrowserException)
            {
                // A page mid-navigation (globals not yet defined) makes the
                // predicate throw -- keep polling until the deadline
                // instead of failing the whole wait immediately, mirroring
                // how Playwright's wait_for_function rides out a live page.
            }

            if (DateTime.UtcNow >= deadline)
            {
                throw new BrowserException($"wait_for_function timed out after {timeoutS}s: {js}");
            }

            await Task.Delay(TimeSpan.FromSeconds(PollIntervalS));
        }
    }

    public async Task ClickAsync(int x, int y)
    {
        if (!_alive) return;
        if (_onScreenPresenter is null && !_warnedNoPresenter)
        {
            _warnedNoPresenter = true;
            CoreLog.Logger.LogWarning(
                "ClickAsync() called with no on-screen presenter configured -- audio autoplay-gate clicks will silently no-op, WebSDR audio may never unlock");
        }

        await Dispatcher.UIThread.InvokeAsync(async () =>
        {
            _onScreenPresenter?.Invoke(true);
            try
            {
                // Visual.PointToScreen(Point) -- an Avalonia extension
                // method (Avalonia.VisualExtensions, confirmed via
                // reflection against the installed package) that resolves
                // a control-local point straight to a screen pixel point,
                // the direct analog of wx's WebView.ClientToScreen().
                var screenPoint = _webView.PointToScreen(new Point(x, y));
                await Task.Delay(30);
                TrustedClick.Click(screenPoint.X, screenPoint.Y);
                // MouseClick's SendInput injection returns before the OS
                // actually delivers the button-down/up messages to the
                // window -- restoring off-screen (or lowering it) on the
                // very next line can move the window out from under the
                // pointer before delivery, so the click lands nowhere and
                // the autoplay gesture never registers. Mirrors
                // WxPageAdapter._simulate_click's own 150ms wait.
                await Task.Delay(150);
            }
            finally
            {
                _onScreenPresenter?.Invoke(false);
            }
        });
    }

    /// <summary>Marks this adapter dead. Does NOT destroy the underlying NativeWebView -- the caller (AvaloniaWebViewHost) owns and destroys that.</summary>
    public Task CloseAsync()
    {
        MarkDead("closed", notify: false);
        _consolePollCts?.Cancel();
        return Task.CompletedTask;
    }

    private void MarkDead(string reason, bool notify = true)
    {
        var wasAlive = _alive;
        _alive = false;
        _consolePollCts?.Cancel();
        if (notify && wasAlive && !_deadReported && _onDead is not null)
        {
            _deadReported = true;
            var cb = _onDead;
            _ = Dispatcher.UIThread.InvokeAsync(() => cb(reason));
        }
    }

    // ------------------------------------------------------------------ console/pageerror polling
    private const string LogArrayJs = "window.__sdrsyncLog";

    private async Task InstallConsoleShimAsync()
    {
        var shim = $$"""
            () => {
                if ({{LogArrayJs}}) return true;
                {{LogArrayJs}} = [];
                var origError = console.error;
                console.error = function() {
                    {{LogArrayJs}}.push({kind:'console', level:'error', text: Array.prototype.slice.call(arguments).join(' ')});
                    return origError.apply(console, arguments);
                };
                var origWarn = console.warn;
                console.warn = function() {
                    {{LogArrayJs}}.push({kind:'console', level:'warning', text: Array.prototype.slice.call(arguments).join(' ')});
                    return origWarn.apply(console, arguments);
                };
                window.addEventListener('error', function(e) {
                    {{LogArrayJs}}.push({kind:'error', text: String((e && (e.error || e.message)) || e)});
                });
                window.addEventListener('unhandledrejection', function(e) {
                    {{LogArrayJs}}.push({kind:'error', text: 'Unhandled promise rejection: ' + String(e.reason)});
                });
                return true;
            }
            """;
        try
        {
            await EvaluateAsync(shim);
        }
        catch (BrowserException e)
        {
            CoreLog.Logger.LogWarning("Could not install console/pageerror log shim: {Message}", e.Message);
        }
    }

    private void StartConsolePolling()
    {
        _consolePollCts?.Cancel();
        var cts = new CancellationTokenSource();
        _consolePollCts = cts;
        _ = PollConsoleLoopAsync(cts.Token);
    }

    private async Task PollConsoleLoopAsync(CancellationToken ct)
    {
        while (_alive && !ct.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(TimeSpan.FromSeconds(ConsoleLogPollIntervalS), ct);
                if (!_alive || ct.IsCancellationRequested) return;
                var drained = await EvaluateAsync($"() => {{ const l = {LogArrayJs} || []; {LogArrayJs} = []; return l; }}");
                if (drained is not { ValueKind: JsonValueKind.Array } array) continue;
                foreach (var entry in array.EnumerateArray())
                {
                    DispatchLogEntry(entry);
                }
            }
            catch (OperationCanceledException)
            {
                return;
            }
            catch (BrowserException)
            {
                // Page mid-navigation or dead -- next poll (or the dead
                // notification) handles it; not worth logging every tick.
            }
        }
    }

    private void DispatchLogEntry(JsonElement entry)
    {
        var kind = entry.TryGetProperty("kind", out var k) ? k.GetString() : null;
        var text = entry.TryGetProperty("text", out var t) ? t.GetString() ?? "" : "";
        if (kind == "console")
        {
            var level = entry.TryGetProperty("level", out var lvl) ? lvl.GetString() ?? "log" : "log";
            var msg = new ConsoleMessage(level, text);
            foreach (var handler in _consoleHandlers.ToArray())
            {
                try { handler(msg); } catch (Exception e) { CoreLog.Logger.LogError(e, "console handler raised"); }
            }
        }
        else if (kind == "error")
        {
            var exc = new BrowserException(text);
            foreach (var handler in _pageErrorHandlers.ToArray())
            {
                try { handler(exc); } catch (Exception e) { CoreLog.Logger.LogError(e, "pageerror handler raised"); }
            }
        }
    }
}
