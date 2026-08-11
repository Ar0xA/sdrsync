using System;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Threading;
using SDRSync.WebSdr;

namespace SDRSync.Gui;

/// <summary>
/// Bridges SyncEngine's background loop to the Avalonia GUI thread for
/// WebView creation/destruction, ported from sdrsync/gui/webview_host.py's
/// WebViewHost class -- but scoped to just that role. NOT ported here
/// (left to the GUI shell / feature-parity steps, since each needs a real
/// MainWindow/CompactWindow pair that doesn't exist yet):
///   - reparent() (spec §9 popout/dock) and present()'s real
///     "raise this window above the desktop" win32 z-order dance
///     (bring_pair_to_front/_kick_to_top/AttachThreadInput) -- both are
///     genuinely GUI-shell-coupled, not WebSDR/browser-layer concerns; the
///     Python original itself keeps them in gui/webview_host.py, a
///     different module from browser_shim.py, for the same reason.
/// AvaloniaWebSdrPage still accepts an on-screen-presenter callback (this
/// class's own Present, currently a no-op placeholder) so that wiring can
/// land later without changing this class's or the page adapter's shape.
///
/// Construct once (mirrors the Python original's "construct in OnInit(),
/// before any window exists" ordering), then call Attach() once a parent
/// panel exists to host the WebView control.
/// </summary>
public sealed class AvaloniaWebViewHost : IWebViewHost
{
    private Panel? _parent;
    private NativeWebView? _currentWebView;

    /// <summary>GUI-thread only. `parent` is the panel the WebView control is added to as a child.</summary>
    public void Attach(Panel parent)
    {
        _parent = parent;
    }

    /// <summary>
    /// Placeholder for WebViewHost.present() -- currently a no-op (no
    /// on-screen z-order dance exists yet, see class doc comment). Passed
    /// to AvaloniaWebSdrPage as its on-screen presenter so a future GUI
    /// shell step can wire in the real behavior without touching the page
    /// adapter.
    /// </summary>
    private void Present(bool onScreen)
    {
    }

    public async Task<IWebSdrPage> CreatePageAsync(Action<string>? onDead = null)
    {
        return await Dispatcher.UIThread.InvokeAsync(() =>
        {
            if (_parent is null)
            {
                throw new InvalidOperationException("AvaloniaWebViewHost.Attach() was never called");
            }

            var webView = new NativeWebView();
            _parent.Children.Add(webView);
            _currentWebView = webView;
            return (IWebSdrPage)new AvaloniaWebSdrPage(webView, Present, onDead);
        });
    }

    public async Task DestroyPageAsync(IWebSdrPage page)
    {
        // Marks the adapter dead first (mirrors WebViewHost.destroy_page())
        // -- matters here for the same underlying reason it did for
        // wx.html2 (calling into a widget mid-teardown is asking for
        // trouble), even though this port's own script-timeout handling
        // doesn't share wx's specific segfault hazard.
        await page.CloseAsync();

        if (page is not AvaloniaWebSdrPage adapter) return;

        await Dispatcher.UIThread.InvokeAsync(() =>
        {
            if (ReferenceEquals(_currentWebView, adapter.WebView))
            {
                _currentWebView = null;
            }

            _parent?.Children.Remove(adapter.WebView);
        });
    }
}
