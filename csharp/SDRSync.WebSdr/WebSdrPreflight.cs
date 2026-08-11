namespace SDRSync.WebSdr;

/// <summary>
/// Lightweight reachability check for a WebSDR site, ported from
/// sdrsync/preflight.py's check_websdr_url() -- the one preflight function
/// the sync engine itself depends on (the attach-retry ladder's cheap
/// gate before spending a full page load). The rest of preflight.py
/// (rigctld/flrig reachability checks, WebSDR type auto-detection, and
/// their GuiMessage result types) is GUI Test-button-triggered only, not
/// needed by the engine, and is deferred to the feature-parity pass step.
/// </summary>
public static class WebSdrPreflight
{
    public const double WebsdrCheckTimeoutS = 5.0;

    /// <summary>
    /// Any HTTP response under 500 counts as "reachable" -- even a 4xx is
    /// still evidence of DNS/routing/the server being up; that's a
    /// different problem than being offline, and is exactly what the
    /// attach ladder's own known-limitation comment already documents
    /// (this gate can't distinguish "down" from "actively refusing us").
    /// </summary>
    public static async Task<(bool Ok, string Message)> CheckWebsdrUrlAsync(string url, double timeoutS = WebsdrCheckTimeoutS)
    {
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(timeoutS) };
        try
        {
            using var response = await http.GetAsync(url, HttpCompletionOption.ResponseHeadersRead);
            var status = (int)response.StatusCode;
            if (status < 500)
            {
                return (true, $"WebSDR site reachable ({url}, HTTP {status})");
            }

            return (false, $"WebSDR site error ({url}, HTTP {status})");
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or UriFormatException)
        {
            return (false, $"Could not reach WebSDR site {url} ({e.Message})");
        }
    }
}
