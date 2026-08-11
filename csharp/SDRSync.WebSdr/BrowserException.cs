namespace SDRSync.WebSdr;

/// <summary>
/// Ported from sdrsync/websdr/browser_shim.py's BrowserError (imported
/// into sync/engine.py as PlaywrightError) -- raised by the embedded-
/// browser page adapter for script/navigation failures. A placeholder
/// forward-declaration here: the concrete WxPageAdapter-equivalent (the
/// Avalonia.Controls.WebView-backed page adapter that actually throws
/// this) lands in the WebSDR/browser layer step; the sync engine's attach
/// supervisor needs the type to exist now to catch it correctly.
/// </summary>
public sealed class BrowserException : Exception
{
    public BrowserException(string message) : base(message)
    {
    }

    public BrowserException(string message, Exception innerException) : base(message, innerException)
    {
    }
}
