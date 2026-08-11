using System.Text.Json;
using Microsoft.Extensions.Logging;
using SDRSync.Core;

namespace SDRSync.WebSdr;

/// <summary>
/// Driver for the UberSDR software family
/// (https://github.com/madpsy/ka9q_ubersdr), ported 1:1 from
/// sdrsync/websdr/ubersdr.py.
///
/// Strategy: unlike the other three drivers, this one does NOT reach into
/// the page's internal globals. UberSDR's v2 interface publishes a
/// documented, versioned control API for exactly this purpose -- the same
/// one its own Chrome and Firefox extensions use -- so this driver is a
/// client of that API and nothing else. The contract is
/// static/v2/BRIDGE_API.md in the UberSDR source tree; its tests there are
/// its specification.
///
/// That difference is worth stating plainly, because it changes what can
/// break: the other three drivers depend on undocumented page internals
/// (ext_tune, demodulators[0], window.ws) -- they work, and
/// WebSDRIncompatibleException exists because those can change without
/// notice. Here, the page promises a stable protocol with a version number
/// and a capability list, refuses bad input with a *reason* instead of
/// clamping, and answers every message -- including "the operator switched
/// the bridge off", which is a specific, reportable state rather than a
/// silence.
///
/// ── The API, in the shape this driver uses it ──────────────────────────
/// Transport: two CustomEvents on window, each carrying one JSON string:
/// ubersdr.to-page (client -> page), ubersdr.from-page (page -> client).
/// Every client message carries {v:1, from:'client', client:&lt;our id&gt;,
/// id:&lt;n&gt;, type:...} and gets exactly one reply. `hello` is answered
/// with `announce` (the receiver's identity, capabilities and topic list).
/// `subscribe` returns a full snapshot of each topic and then pushes only
/// what changed. `command` returns the state it just set, so no follow-up
/// read is needed.
///
/// The commands used here, and why each: tune {frequency, ensureVisible}
/// (the dial); tune {mode, bandwidthLow, bandwidthHigh} (mode and filter
/// in ONE call -- sent separately, the receiver passes through the new
/// mode's *default* filter on the way to the one we wanted, and that is
/// audible); duck {ducked} (transient silence -- see SetMutedAsync); bye
/// (release our client slot).
///
/// ── Why an injected agent, and why one round trip ──────────────────────
/// The transport is event-driven and IWebSdrPage.EvaluateAsync() is
/// request/response over JSON, so a tiny agent is installed in the page
/// (see AgentJs) to hold the client id, the merged topic state and the
/// replies. Everything below then reads or drives that agent. The page's
/// host handles a message *synchronously* inside our dispatchEvent call,
/// so the ordinary case is one evaluate: dispatch, and the reply is
/// already in hand when dispatchEvent returns. CallAsync still polls,
/// because a command *may* answer asynchronously and the protocol permits
/// it -- but the poll is the exception, not the normal cost.
/// </summary>
public sealed class UberSDRDriver : IWebSDRDriver
{
    public static readonly string[] FingerprintMarkers = { "dist/v2.js", "browser-extension-detector.js" };

    private const double LoadTimeoutS = 20.0;
    private const double CallTimeoutS = 3.0;
    private const double CallPollS = 0.05;
    private const int AudioStartAttempts = 2;
    private const double AudioStartWaitS = 1.5;
    private const double RetryHandshakeS = 2.0;
    private const int ProtocolV = 1;
    private const int ApiMajor = 1;

    // hamlib mode name -> UberSDR mode id. From the receiver's own mode
    // table (static/v2/src/radio/constants.js): lsb, usb, am, sam, nfm,
    // fm, cwl, cwu.
    //
    // CW is the one that needs care. UberSDR has cwu and cwl, and the
    // letter says which sideband the *tone* is on -- the filter is
    // symmetric about the dial either way. hamlib's CW/CWR carry the same
    // distinction, so they map straight across rather than both collapsing
    // onto one.
    private static readonly Dictionary<string, string> ModeMap = new()
    {
        ["USB"] = "usb",
        ["PKTUSB"] = "usb",
        ["DATA-U"] = "usb",
        ["LSB"] = "lsb",
        ["PKTLSB"] = "lsb",
        ["DATA-L"] = "lsb",
        ["CW"] = "cwu",
        ["CW-U"] = "cwu",
        ["CWR"] = "cwl",
        ["CW-L"] = "cwl",
        ["AM"] = "am",
        ["AMS"] = "sam",
        ["SAM"] = "sam",
        ["FM"] = "nfm",
        ["FMN"] = "nfm",
        ["WFM"] = "fm",
        ["FM-WIDE"] = "fm",
    };

    public sealed record ModeInfo(int Low, int High, int Min, int Max, string Sideband);

    // Fallback passband edges and limits, used only if the page's own
    // `modes` topic could not be read. The live table is preferred (see
    // LoadModesAsync) so a receiver whose limits differ from this build is
    // still driven correctly.
    private static readonly Dictionary<string, ModeInfo> FallbackModes = new()
    {
        ["usb"] = new ModeInfo(50, 2700, 0, 6000, "upper"),
        ["lsb"] = new ModeInfo(-2700, -50, -6000, 0, "lower"),
        ["am"] = new ModeInfo(-5000, 5000, -8000, 8000, "both"),
        ["sam"] = new ModeInfo(-5000, 5000, -8000, 8000, "both"),
        ["nfm"] = new ModeInfo(-5000, 5000, -8000, 8000, "both"),
        ["fm"] = new ModeInfo(-8000, 8000, -12000, 12000, "both"),
        ["cwu"] = new ModeInfo(-200, 200, -500, 500, "both"),
        ["cwl"] = new ModeInfo(-200, 200, -500, 500, "both"),
    };

    /// <summary>Pure mapping from a hamlib mode name to an UberSDR mode id. Null when there is no equivalent (the caller logs and skips mode sync, leaving frequency sync unaffected). No passband argument, unlike KiwiSDR's mapper: UberSDR takes the filter as real numbers -- see PassbandEdges().</summary>
    public static string? MapHamlibModeUbersdr(string hamlibMode) => ModeMap.GetValueOrDefault(hamlibMode.ToUpperInvariant());

    // Reverse of ModeMap's collapsing cases, for reverse sync (WebSDR ->
    // rig, v11). GetStatusAsync() reports the receiver's mode id
    // uppercased, so the keys here are upper too. One canonical hamlib
    // name per UberSDR id: nfm collapses FM/FMN's many hamlib spellings
    // back to the plain one, and cwu/cwl keep CW/CWR distinct because the
    // API (and IsCw() below) does.
    private static readonly Dictionary<string, string> ReverseModeMap = new()
    {
        ["USB"] = "USB",
        ["LSB"] = "LSB",
        ["AM"] = "AM",
        ["SAM"] = "SAM",
        ["NFM"] = "FM",
        ["FM"] = "WFM",
        ["CWU"] = "CW",
        ["CWL"] = "CWR",
    };

    /// <summary>Pure reverse mapping: GetStatusAsync()-normalized mode string (already uppercased) -> a canonical hamlib mode name. Null if unknown.</summary>
    public static string? MapUbersdrModeToHamlib(string? mode) => mode is null ? null : ReverseModeMap.GetValueOrDefault(mode.ToUpperInvariant());

    /// <summary>
    /// The filter edges to ask for, in Hz relative to the dial, or null.
    ///
    /// Null means "say nothing about the filter", which leaves the
    /// receiver on the mode's own default -- the right answer when the rig
    /// did not report a passband, or reported something the mode cannot
    /// do.
    ///
    /// UberSDR's filter is two edges relative to the tuned frequency, and
    /// which edges a width becomes depends on the mode's sideband: upper
    /// (usb) -- the low edge stays where the mode puts it (50 Hz, so the
    /// filter does not open onto the carrier) and the width is added above
    /// it; lower (lsb) -- the mirror of that; both (cw, am, sam, fm) --
    /// symmetric about the dial: a 500 Hz rig filter is -250..+250,
    /// because that is what a symmetric filter of that width *is*.
    ///
    /// Edges are clamped into the mode's limits rather than sent and
    /// refused: a rig with a 3.5 kHz SSB filter on a receiver that allows
    /// 6 kHz is fine, and one with a 15 kHz FM filter on a receiver that
    /// allows 12 is asking for the widest the receiver has, which is the
    /// honest reading of "as wide as I am".
    /// </summary>
    public static (int Low, int High)? PassbandEdges(string modeId, int? widthHz, Dictionary<string, ModeInfo>? modes = null)
    {
        var table = modes ?? FallbackModes;
        if (!table.TryGetValue(modeId, out var info) || widthHz is null or <= 0) return null;

        int low, high;
        if (info.Sideband == "upper")
        {
            low = info.Low;
            high = low + widthHz.Value;
        }
        else if (info.Sideband == "lower")
        {
            high = info.High;
            low = high - widthHz.Value;
        }
        else
        {
            var half = (int)Math.Round(widthHz.Value / 2.0);
            (low, high) = (-half, half);
        }

        low = Math.Max(info.Min, Math.Min(info.Max, low));
        high = Math.Max(info.Min, Math.Min(info.Max, high));
        // A clamp can collapse the two edges onto each other (a width of 1
        // Hz, or a limit table that disagrees with the defaults). A filter
        // of no width is not a filter, so fall back to the mode's own
        // rather than sending nonsense the receiver would refuse.
        if (high - low < 10) return null;
        return (low, high);
    }

    /// <summary>
    /// The v2 interface's URL for a site URL somebody pasted. Only the v2
    /// interface has the control API this driver speaks -- the older one
    /// has no such thing -- and an operator pasting their receiver's
    /// address will paste the root. So `https://rx.example/` becomes
    /// `https://rx.example/v2/`, while a URL already pointing at v2 is left
    /// alone. The query string is preserved, because UberSDR's own share
    /// links carry the frequency and mode in it and pasting one should
    /// land where it says.
    /// </summary>
    public static string V2PageUrl(string url)
    {
        var normalized = url.Contains("//") ? url : $"http://{url}";
        var uri = new Uri(normalized);
        var path = string.IsNullOrEmpty(uri.AbsolutePath) ? "/" : uri.AbsolutePath;
        var segments = path.Split('/', StringSplitOptions.RemoveEmptyEntries);

        if (segments.Length > 0 && segments[^1] == "v2")
        {
            var newPath = path.EndsWith("/") ? path : path + "/";
            return new UriBuilder(uri) { Path = newPath }.Uri.ToString();
        }

        if (segments.Contains("v2"))
        {
            // Already inside the v2 interface somewhere (a sub-path, or a
            // URL with a file on the end). Left exactly as given.
            return uri.ToString();
        }

        return new UriBuilder(uri) { Path = path.TrimEnd('/') + "/v2/" }.Uri.ToString();
    }

    // The in-page agent. Installed once per page load, idempotent, and
    // deliberately small: it is a transport, not logic. Everything it
    // knows is either the protocol's own vocabulary or something the page
    // told it. Verbatim from ubersdr.py's _AGENT_JS -- this is real
    // browser-side JS driving the page's actual control protocol, not
    // C#-specific code, so it is copied unchanged rather than "ported".
    private const string AgentJs = """
        () => {
            var KEY = '__sdrsyncUberSDR';
            if (window[KEY] && window[KEY].v === 1) {
                return {installed: true, ready: window[KEY].ready, refused: window[KEY].refused};
            }
            var agent = {
                v: 1,
                client: 'sdrsync-' + Math.random().toString(36).slice(2, 10),
                nextId: 1,
                ready: false,
                refused: null,
                descriptor: null,
                topics: {},
                results: {},
                closed: false,
            };
            window[KEY] = agent;

            window.addEventListener('ubersdr.from-page', function (ev) {
                var msg;
                try { msg = JSON.parse(ev.detail); } catch (e) { return; }
                if (!msg || msg.v !== 1 || msg.from !== 'page') return;
                if (msg.client && msg.client !== agent.client) return;

                if (msg.type === 'announce') {
                    agent.descriptor = msg;
                    agent.topics = {};
                    agent.ready = true;
                    agent.refused = null;
                    agent.closed = false;
                    agent.resubscribe();
                    return;
                }
                if (msg.type === 'state') {
                    var cur = agent.topics[msg.topic] || {};
                    var patch = msg.patch || {};
                    for (var k in patch) cur[k] = patch[k];
                    agent.topics[msg.topic] = cur;
                    return;
                }
                if (msg.type === 'result') {
                    if (agent.seedIds[msg.id]) {
                        delete agent.seedIds[msg.id];
                        if (msg.ok) agent.seed(msg.value);
                        return;
                    }
                    agent.results[msg.id] = {ok: !!msg.ok, value: msg.value, error: msg.error || null};
                    if (!msg.ok && msg.error && msg.error.code === 'disabled') {
                        agent.refused = msg.error.message || 'the browser bridge is switched off';
                    }
                    return;
                }
                if (msg.type === 'closing') {
                    agent.closed = true;
                    agent.ready = false;
                }
            });

            agent.emit = function (id, type, fields) {
                var msg = {v: 1, from: 'client', client: agent.client, id: id, type: type};
                for (var k in (fields || {})) msg[k] = fields[k];
                window.dispatchEvent(new CustomEvent('ubersdr.to-page', {detail: JSON.stringify(msg)}));
                return id;
            };

            agent.send = function (type, fields) {
                return agent.emit(agent.nextId++, type, fields);
            };

            agent.call = function (type, fields) {
                var id = agent.send(type, fields);
                var res = agent.results[id];
                if (res) { delete agent.results[id]; return {id: id, done: true, res: res}; }
                return {id: id, done: false, res: null};
            };

            agent.subscription = null;
            agent.seedIds = {};
            agent.resubscribe = function () {
                if (!agent.subscription) return;
                var id = agent.nextId++;
                agent.seedIds[id] = true;
                agent.emit(id, 'subscribe', agent.subscription);
            };

            agent.take = function (id) {
                var res = agent.results[id];
                if (!res) return null;
                delete agent.results[id];
                return res;
            };

            agent.seed = function (value) {
                for (var t in (value || {})) agent.topics[t] = value[t];
            };

            return {installed: true, ready: agent.ready, refused: agent.refused};
        }
        """;

    private const string Key = "window.__sdrsyncUberSDR";

    public string Url { get; }
    internal int CwOffsetHz { get; }

    internal IWebSdrPage? _page;
    internal bool _attached;
    private bool _listenersRegistered;
    internal string? _currentMode;
    private Dictionary<string, ModeInfo>? _modes;
    private Dictionary<string, JsonElement> _receiver = new();
    private string[] _capabilities = Array.Empty<string>();
    // Whether this receiver has the transient-silence command. Read from
    // the announce, not assumed -- see SetMutedAsync.
    internal bool _canDuck = true;
    internal string? _lastAttachError;
    internal string? _lastPageError;
    internal string? _lastTuneError;
    internal string? _lastModeError;
    private string? _lastUnmappedMode;
    private bool _audioStarted;

    public UberSDRDriver(string url, int cwOffsetHz = 0)
    {
        Url = V2PageUrl(url);
        CwOffsetHz = cwOffsetHz;
    }

    public bool Attached => _attached;

    // ------------------------------------------------------------------ attach
    public async Task AttachAsync(IWebSdrPage page)
    {
        _page = page;
        if (!_listenersRegistered)
        {
            page.OnConsole(OnConsole);
            page.OnPageError(OnPageError);
            _listenersRegistered = true;
        }

        // A reattach on a page that is still the right page should not
        // reload it. Reloading costs the operator's audio session and
        // everything the page had set up, and a reattach is not always the
        // page's fault: we can be let go to make room (the page keeps at
        // most eight clients and evicts the stalest), or a single script
        // call can have timed out. So: say hello to what is already there,
        // and only navigate if that gets no answer.
        if (!await HandshakeAsync(RetryHandshakeS))
        {
            try
            {
                await page.NavigateAsync(Url, LoadTimeoutS);
            }
            catch (BrowserException e)
            {
                _lastAttachError = $"Could not load {Url}: {e.Message}";
                throw new WebSDRIncompatibleException(_lastAttachError);
            }

            if (!await HandshakeAsync(LoadTimeoutS))
            {
                _lastAttachError =
                    $"Page at {Url} did not answer the UberSDR v2 control API within {LoadTimeoutS}s. " +
                    "Is this an UberSDR running the v2 interface?";
                throw new WebSDRIncompatibleException(_lastAttachError);
            }
        }

        var refused = await AgentFieldAsync("refused");
        if (refused is { ValueKind: JsonValueKind.String } refusedStr)
        {
            // A specific, actionable state rather than a mystery: the
            // operator has a switch for this API and it is off.
            _lastAttachError =
                $"UberSDR at {Url} refused control: {refusedStr.GetString()}. Switch it on in the " +
                "receiver's SDR Control panel (Browser bridge).";
            throw new WebSDRIncompatibleException(_lastAttachError);
        }

        await ReadDescriptorAsync();
        await SubscribeAsync();
        await LoadModesAsync();

        _attached = true;
        _currentMode = null;
        _audioStarted = false;
        _lastAttachError = null;
        _lastPageError = null;
        _lastTuneError = null;
        _lastModeError = null;
        _lastUnmappedMode = null;

        var name = GetStringOrNull(_receiver, "name") ?? GetStringOrNull(_receiver, "callsign") ?? "UberSDR";
        CoreLog.Logger.LogInformation(
            "Attached to {Name} ({Url}) via the v2 control API; capabilities: {Capabilities}",
            name, Url, _capabilities.Length > 0 ? string.Join(", ", _capabilities) : "none reported");

        // Last, and never fatal: the receiver is controllable without
        // audio, and a rig that retunes a silent WebSDR is still doing its
        // job.
        await StartAudioAsync();
    }

    /// <summary>
    /// Install the agent, say hello, and wait for the page to answer. This
    /// is the identification, and it is the same one the browser
    /// extensions do: a page that answers with an `announce` *is* an
    /// UberSDR with a working control API, and one that does not is not,
    /// whatever its HTML serves. A script-tag fingerprint can only guess at
    /// that; this settles it. `refused` counts as an answer -- the bridge
    /// being switched off is the page talking to us, and reloading would
    /// not change its mind.
    /// </summary>
    private async Task<bool> HandshakeAsync(double timeoutS)
    {
        try
        {
            await InstallAgentAsync();
            await _page!.WaitForFunctionAsync($"!!({Key} && ({Key}.ready || {Key}.refused))", timeoutS);
            return true;
        }
        catch (BrowserException)
        {
            return false;
        }
    }

    private async Task InstallAgentAsync()
    {
        await _page!.EvaluateAsync(AgentJs);
        // `hello` is answered with an announce addressed to us. Sent every
        // attach, including a reattach on an already-loaded page: the page
        // may have announced before the agent existed.
        await _page.EvaluateAsync($"() => {{ {Key}.send('hello'); return true; }}");
    }

    private async Task<JsonElement?> AgentFieldAsync(string name)
    {
        try
        {
            return await _page!.EvaluateAsync($"() => ({Key} ? {Key}.{name} : null)");
        }
        catch (BrowserException)
        {
            return null;
        }
    }

    private async Task ReadDescriptorAsync()
    {
        var d = await AgentFieldAsync("descriptor");
        var dObj = d is { ValueKind: JsonValueKind.Object } ? d.Value : default;
        JsonElement api = default;
        var hasApi = dObj.ValueKind == JsonValueKind.Object && dObj.TryGetProperty("api", out api) && api.ValueKind == JsonValueKind.Object;

        int? v = dObj.ValueKind == JsonValueKind.Object && dObj.TryGetProperty("v", out var vProp) && vProp.ValueKind == JsonValueKind.Number ? vProp.GetInt32() : null;
        int? apiMajor = hasApi && api.TryGetProperty("major", out var majProp) && majProp.ValueKind == JsonValueKind.Number ? majProp.GetInt32() : null;
        int? apiMinor = hasApi && api.TryGetProperty("minor", out var minProp) && minProp.ValueKind == JsonValueKind.Number ? minProp.GetInt32() : null;

        // The protocol's own compatibility rule: check the envelope
        // version and the API major, feature-detect the rest on
        // capabilities. A major bump means the meanings changed, which is
        // the one thing this cannot adapt to on its own.
        if ((v is not null && v != ProtocolV) || (apiMajor is not null && apiMajor != ApiMajor))
        {
            _lastAttachError =
                $"UberSDR at {Url} speaks protocol v{v?.ToString() ?? "None"} API {apiMajor?.ToString() ?? "None"}." +
                $"{apiMinor?.ToString() ?? "None"}; this driver speaks v{ProtocolV} API {ApiMajor}.x. Update sdrsync.";
            throw new WebSDRIncompatibleException(_lastAttachError);
        }

        _receiver = dObj.ValueKind == JsonValueKind.Object && dObj.TryGetProperty("receiver", out var recv) && recv.ValueKind == JsonValueKind.Object
            ? recv.EnumerateObject().ToDictionary(p => p.Name, p => p.Value)
            : new Dictionary<string, JsonElement>();

        _capabilities = dObj.ValueKind == JsonValueKind.Object && dObj.TryGetProperty("capabilities", out var caps) && caps.ValueKind == JsonValueKind.Array
            ? caps.EnumerateArray().Select(c => c.ValueKind == JsonValueKind.String ? c.GetString()! : c.ToString()).ToArray()
            : Array.Empty<string>();

        // An empty list means an announce we could not read, not a
        // receiver with no commands -- so the good case is assumed and a
        // refusal is reported rather than a silence being introduced by
        // our own guess.
        _canDuck = _capabilities.Length > 0 ? _capabilities.Contains("duck") : true;
        if (_capabilities.Length > 0 && !_canDuck)
        {
            CoreLog.Logger.LogWarning(
                "UberSDR at {Url} has no `duck` command; transmit silence will use the operator's own mute instead", Url);
        }
    }

    private static string? GetStringOrNull(Dictionary<string, JsonElement> dict, string key) =>
        dict.TryGetValue(key, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    /// <summary>
    /// Subscribe to what GetStatusAsync() reports, and seed it with a
    /// snapshot. Three topics and not `signal`: the meter changes
    /// continuously and nothing here shows it, so asking for it would be
    /// ten messages a second into a variable nobody reads.
    /// </summary>
    private async Task SubscribeAsync()
    {
        var topics = new[] { "tuning", "audio", "session" };
        // Remembered in the page, so the agent can re-send it the moment
        // the page announces again -- an announce resets what the page
        // thinks we have been told, and patches after it are diffs against
        // nothing.
        await _page!.EvaluateAsync($"(t) => {{ {Key}.subscription = {{topics: t}}; return true; }}", (object)topics);
        var res = await CallAsync("subscribe", null, new Dictionary<string, object?> { ["topics"] = topics });
        if (IsOk(res))
        {
            var value = ValueOf(res) ?? JsonSerializer.SerializeToElement(new Dictionary<string, object>());
            await _page.EvaluateAsync($"(v) => {{ {Key}.seed(v); return true; }}", (object)value);
        }
    }

    /// <summary>
    /// The receiver's own mode table, for turning a rig passband into
    /// edges. Read from the page rather than hardcoded so a receiver whose
    /// limits differ from this build's assumptions is still driven
    /// correctly. A failure here is not fatal: FallbackModes covers it,
    /// and the worst case is a filter the receiver refuses with a reason.
    /// </summary>
    private async Task LoadModesAsync()
    {
        var res = await CallAsync("get", null, new Dictionary<string, object?> { ["topic"] = "modes" });
        if (!IsOk(res) || ValueOf(res) is not { ValueKind: JsonValueKind.Array } rows)
        {
            CoreLog.Logger.LogDebug("Could not read the receiver's mode table; using built-in defaults");
            return;
        }

        var table = new Dictionary<string, ModeInfo>();
        foreach (var row in rows.EnumerateArray())
        {
            if (row.ValueKind != JsonValueKind.Object || !row.TryGetProperty("id", out var idProp) || idProp.ValueKind != JsonValueKind.String) continue;
            var id = idProp.GetString()!;
            var def = row.TryGetProperty("default", out var d) && d.ValueKind == JsonValueKind.Object ? d : default;
            var limits = row.TryGetProperty("limits", out var l) && l.ValueKind == JsonValueKind.Object ? l : default;
            var sideband = limits.ValueKind == JsonValueKind.Object && limits.TryGetProperty("sideband", out var sb) && sb.ValueKind == JsonValueKind.String ? sb.GetString()! : "both";
            table[id] = new ModeInfo(
                GetIntOrDefault(def, "low", 0),
                GetIntOrDefault(def, "high", 0),
                GetIntOrDefault(limits, "min", 0),
                GetIntOrDefault(limits, "max", 0),
                sideband);
        }

        if (table.Count > 0) _modes = table;
    }

    private static int GetIntOrDefault(JsonElement obj, string prop, int fallback) =>
        obj.ValueKind == JsonValueKind.Object && obj.TryGetProperty(prop, out var v) && v.ValueKind == JsonValueKind.Number ? (int)v.GetDouble() : fallback;

    /// <summary>
    /// Press Start, because the receiver will not do it for us. `power
    /// {on:true}` is refused by design -- browsers require a user gesture
    /// to begin playback -- so the page shows a Start overlay and waits.
    /// Two attempts: the button's own click first, and a real pointer
    /// click at its coordinates second, which is what the other drivers
    /// use to satisfy an autoplay gate. Never fatal: frequency and mode
    /// sync work on a silent receiver, and reporting audio_active=false is
    /// more useful than refusing to attach.
    /// </summary>
    private async Task StartAudioAsync()
    {
        for (var attempt = 0; attempt < AudioStartAttempts; attempt++)
        {
            if (await RunningAsync())
            {
                _audioStarted = true;
                return;
            }

            JsonElement? box;
            try
            {
                box = await _page!.EvaluateAsync(
                    "() => { var b = document.querySelector('.start__go');" +
                    " if (!b) return null;" +
                    " var r = b.getBoundingClientRect();" +
                    " return {x: r.left + r.width / 2, y: r.top + r.height / 2}; }");
            }
            catch (BrowserException)
            {
                box = null;
            }

            if (box is not { ValueKind: JsonValueKind.Object })
            {
                // No overlay: either it is already running (caught above
                // on the next pass) or this page does not gate audio.
                await Task.Delay(TimeSpan.FromSeconds(AudioStartWaitS));
                continue;
            }

            try
            {
                if (attempt == 0)
                {
                    await _page!.EvaluateAsync(
                        "() => { var b = document.querySelector('.start__go');" +
                        " if (b) b.click(); return true; }");
                }
                else
                {
                    var x = (int)box.Value.GetProperty("x").GetDouble();
                    var y = (int)box.Value.GetProperty("y").GetDouble();
                    await _page!.ClickAsync(x, y);
                }
            }
            catch (BrowserException e)
            {
                CoreLog.Logger.LogDebug("Could not press the receiver's Start button: {Message}", e.Message);
            }

            await Task.Delay(TimeSpan.FromSeconds(AudioStartWaitS));
        }

        _audioStarted = await RunningAsync();
        if (!_audioStarted)
        {
            CoreLog.Logger.LogWarning(
                "UberSDR at {Url} is tuned and controllable but its audio has not started " +
                "(the page's Start button did not take). Frequency and mode sync are unaffected.", Url);
        }
    }

    private async Task<bool> RunningAsync()
    {
        var session = await TopicAsync("session");
        return session.TryGetProperty("running", out var r) && r.ValueKind is JsonValueKind.True;
    }

    // ------------------------------------------------------------------- tuning
    public async Task<bool> TuneHzAsync(int freqHz, bool verify = true)
    {
        // No readback: the command returns the tuning it just set, and an
        // impossible frequency comes back as `bad_args` with a reason
        // rather than being clamped silently. That is stricter than a
        // readback comparison and the reason gets logged. verify is
        // accepted for IWebSDRDriver Protocol parity but unused here.
        if (!_attached) return false;
        var effectiveHz = freqHz + (IsCw() ? CwOffsetHz : 0);

        var res = await CallAsync("command", "tune", new Dictionary<string, object?>
        {
            ["name"] = "tune",
            ["args"] = new Dictionary<string, object?> { ["frequency"] = effectiveHz, ["ensureVisible"] = true },
        });
        if (res is null) return false;
        if (!IsOk(res))
        {
            _lastTuneError = ErrorText("tune", res);
            CoreLog.Logger.LogWarning("{Message}", _lastTuneError);
            return false;
        }

        _lastTuneError = null;
        return true;
    }

    /// <summary>
    /// Mode and filter in one command. Returns true only if applied. One
    /// command and not two: `mode` alone would set the mode's default
    /// filter and a following `passband` would replace it, so the receiver
    /// would pass audibly through the wrong width. `tune` takes both
    /// together, which is what the API documents it for.
    /// </summary>
    public async Task<bool> SetModeAsync(string hamlibMode, int? passbandHz)
    {
        if (!_attached) return false;
        var modeId = MapHamlibModeUbersdr(hamlibMode);
        if (modeId is null)
        {
            _lastModeError = $"hamlib mode '{hamlibMode}' has no UberSDR equivalent; frequency sync continues";
            if (hamlibMode != _lastUnmappedMode)
            {
                _lastUnmappedMode = hamlibMode;
                CoreLog.Logger.LogWarning("{Message}", _lastModeError);
            }
            else
            {
                CoreLog.Logger.LogDebug("{Message}", _lastModeError);
            }

            return false;
        }

        var args = new Dictionary<string, object?> { ["mode"] = modeId };
        var edges = PassbandEdges(modeId, passbandHz, _modes);
        if (edges is not null)
        {
            args["bandwidthLow"] = edges.Value.Low;
            args["bandwidthHigh"] = edges.Value.High;
        }

        var res = await CallAsync("command", "tune", new Dictionary<string, object?> { ["name"] = "tune", ["args"] = args });
        if (res is null) return false;
        if (!IsOk(res))
        {
            _lastModeError = ErrorText("mode", res);
            CoreLog.Logger.LogWarning("{Message}", _lastModeError);
            // A refused *filter* must not cost us the mode change: retry
            // with the mode alone, which leaves the receiver on its own
            // default width.
            if (edges is not null)
            {
                var retry = await CallAsync("command", "tune", new Dictionary<string, object?>
                {
                    ["name"] = "tune", ["args"] = new Dictionary<string, object?> { ["mode"] = modeId },
                });
                if (IsOk(retry))
                {
                    CoreLog.Logger.LogInformation(
                        "UberSDR refused the rig's {Width} Hz filter for {Mode}; used the receiver's own passband instead",
                        passbandHz ?? 0, modeId);
                    _currentMode = modeId;
                    _lastModeError = null;
                    _lastUnmappedMode = null;
                    return true;
                }
            }

            return false;
        }

        _currentMode = modeId;
        _lastModeError = null;
        _lastUnmappedMode = null;
        return true;
    }

    /// <summary>
    /// Silence for transmit -- with `duck` where there is one, and that is
    /// the point. UberSDR draws a distinction the other families do not:
    /// `mute` is the operator's own setting, which the page's mute button
    /// shows and that browser remembers between visits, while `duck` is
    /// transient silence applied by something else. Transmit is the second
    /// kind. Using `duck` means sdrsync dying mid-transmission leaves
    /// nothing behind to undo, and the mute button in the view never shows
    /// a state nobody chose. Feature-detected on the announce's capability
    /// list rather than assumed. Absolute, never a toggle: PTT arrives as
    /// "transmitting: true/false", and a toggle desynchronises permanently
    /// the first time a message is missed.
    /// </summary>
    public async Task SetMutedAsync(bool muted)
    {
        if (!_attached) return;
        var name = _canDuck ? "duck" : "mute";
        var args = _canDuck
            ? new Dictionary<string, object?> { ["ducked"] = muted }
            : new Dictionary<string, object?> { ["muted"] = muted };
        var res = await CallAsync("command", name, new Dictionary<string, object?> { ["name"] = name, ["args"] = args });
        if (res is not null && !IsOk(res))
        {
            CoreLog.Logger.LogDebug("{Name} command refused: {Message}", name, ErrorText(name, res));
        }
    }

    // ------------------------------------------------------------------- status
    public async Task<WebSDRStatus> GetStatusAsync()
    {
        if (!_attached) return new WebSDRStatus(Connected: false, LastError: CombinedError());

        JsonElement? state;
        try
        {
            state = await _page!.EvaluateAsync(
                $"() => ({Key} ? {{ready: {Key}.ready, closed: {Key}.closed," +
                $" refused: {Key}.refused, topics: {Key}.topics}} : null)");
        }
        catch (BrowserException e)
        {
            _attached = false;
            _lastPageError = e.Message;
            return new WebSDRStatus(Connected: false, LastError: CombinedError());
        }

        if (state is not { ValueKind: JsonValueKind.Object } stateObj)
        {
            // The agent is gone, which means the page reloaded under us.
            // Drop attachment so the engine's supervisor re-attaches
            // rather than driving a page that is no longer listening.
            _attached = false;
            _lastPageError = "The UberSDR page reloaded; re-attaching";
            return new WebSDRStatus(Connected: false, LastError: CombinedError());
        }

        var closed = stateObj.TryGetProperty("closed", out var c) && c.ValueKind is JsonValueKind.True;
        var ready = stateObj.TryGetProperty("ready", out var r) && r.ValueKind is JsonValueKind.True;
        if (closed || !ready)
        {
            _attached = false;
            var refused = stateObj.TryGetProperty("refused", out var ref_) && ref_.ValueKind == JsonValueKind.String ? ref_.GetString() : null;
            _lastPageError = refused ?? "The UberSDR page closed the control connection; re-attaching";
            return new WebSDRStatus(Connected: false, LastError: CombinedError());
        }

        var topics = stateObj.TryGetProperty("topics", out var t) && t.ValueKind == JsonValueKind.Object ? t : default;
        var tuning = GetSubObject(topics, "tuning");
        var session = GetSubObject(topics, "session");
        var audio = GetSubObject(topics, "audio");

        int? freqHz = tuning.ValueKind == JsonValueKind.Object && tuning.TryGetProperty("frequency", out var f) && f.ValueKind == JsonValueKind.Number
            ? (int)f.GetDouble()
            : null;
        string? mode = tuning.ValueKind == JsonValueKind.Object && tuning.TryGetProperty("mode", out var m) && m.ValueKind == JsonValueKind.String
            ? m.GetString()!.ToUpperInvariant()
            : null;
        var running = session.ValueKind == JsonValueKind.Object && session.TryGetProperty("running", out var run) && run.ValueKind is JsonValueKind.True;
        var ducked = audio.ValueKind == JsonValueKind.Object && audio.TryGetProperty("ducked", out var duck) && duck.ValueKind is JsonValueKind.True;

        return new WebSDRStatus(
            Connected: true,
            FreqHz: freqHz,
            Mode: mode,
            // The receiver's own answer to "is audio playing", pushed to
            // us rather than inferred from a socket's readyState as the
            // other drivers must. `ducked` is folded in because a ducked
            // receiver is silent, and reporting it as playing would be a
            // lie a user can hear.
            AudioActive: running && !ducked,
            LastError: CombinedError());
    }

    private static JsonElement GetSubObject(JsonElement topics, string name) =>
        topics.ValueKind == JsonValueKind.Object && topics.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Object ? v : default;

    public async Task CloseAsync()
    {
        // Say goodbye, so the slot is freed rather than waiting to be
        // evicted: the page holds at most eight clients and evicts the
        // stalest to make room. A session that reconnects repeatedly
        // without saying bye would push out somebody's browser extension.
        if (_page is not null && _attached)
        {
            try
            {
                await _page.EvaluateAsync($"() => {{ {Key} && {Key}.send('bye'); return true; }}");
            }
            catch (BrowserException)
            {
            }
        }

        _attached = false;
        _page = null;
    }

    /// <summary>
    /// Un-applies CwOffsetHz for the reverse direction (WebSDR -> rig),
    /// symmetric to TuneHzAsync's forward application above. Takes the
    /// mode as an explicit argument, sourced from the SAME status snapshot
    /// MapUbersdrModeToHamlib() derived it from -- NOT _currentMode, which
    /// only reflects this driver's own last PUSHED mode and is stale by
    /// construction for reverse sync (a page change the driver didn't
    /// itself push, e.g. someone else on the receiver retuning it, is
    /// exactly what reverse sync exists to observe).
    /// </summary>
    private int? ReverseEffectiveHz(int? observedHz, string? observedHamlibMode) =>
        observedHz is null ? null : observedHamlibMode is "CW" or "CWR" ? observedHz - CwOffsetHz : observedHz;

    public string? HamlibModeFromStatus(WebSDRStatus status) => MapUbersdrModeToHamlib(status.Mode);

    public int? RigFreqFromStatus(WebSDRStatus status) => ReverseEffectiveHz(status.FreqHz, HamlibModeFromStatus(status));

    // ---------------------------------------------------------------- internals
    private bool IsCw() => _currentMode is "cwu" or "cwl";

    private async Task<JsonElement> TopicAsync(string name)
    {
        try
        {
            var got = await _page!.EvaluateAsync($"() => ({Key} && {Key}.topics ? ({Key}.topics['{name}'] || null) : null)");
            return got is { ValueKind: JsonValueKind.Object } ? got.Value : default;
        }
        catch (BrowserException)
        {
            return default;
        }
    }

    /// <summary>
    /// Send one message and return its reply, or null if it never came.
    /// The page answers a synchronous command inside our own dispatch, so
    /// the common case is settled by the first evaluate and the loop below
    /// never runs. It exists because the protocol allows an asynchronous
    /// answer, and a client that assumed otherwise would work until the
    /// day one command started returning a promise.
    /// </summary>
    private async Task<JsonElement?> CallAsync(string msgType, string? label, Dictionary<string, object?> fields)
    {
        if (_page is null) return null;

        JsonElement? first;
        try
        {
            first = await _page.EvaluateAsync(
                $"(f) => {Key}.call(f.type, f.fields)",
                (object)new Dictionary<string, object?> { ["type"] = msgType, ["fields"] = fields });
        }
        catch (BrowserException e)
        {
            NoteCallFailure(label, $"could not reach the UberSDR page: {e.Message}");
            return null;
        }

        if (first is not { ValueKind: JsonValueKind.Object } firstObj)
        {
            NoteCallFailure(label, "the in-page control agent is missing");
            return null;
        }

        if (firstObj.TryGetProperty("done", out var done) && done.ValueKind is JsonValueKind.True)
        {
            return firstObj.TryGetProperty("res", out var res) ? res : null;
        }

        var callId = firstObj.TryGetProperty("id", out var idProp) ? idProp : default;
        var deadline = DateTime.UtcNow.AddSeconds(CallTimeoutS);
        while (DateTime.UtcNow < deadline)
        {
            await Task.Delay(TimeSpan.FromSeconds(CallPollS));
            JsonElement? res;
            try
            {
                res = await _page.EvaluateAsync($"(id) => {Key}.take(id)", (object)callId);
            }
            catch (BrowserException e)
            {
                NoteCallFailure(label, $"could not reach the UberSDR page: {e.Message}");
                return null;
            }

            if (res is { ValueKind: JsonValueKind.Object }) return res;
        }

        NoteCallFailure(label, $"no reply within {CallTimeoutS:F0}s");
        return null;
    }

    private static bool IsOk(JsonElement? res) => res is { ValueKind: JsonValueKind.Object } o && o.TryGetProperty("ok", out var ok) && ok.ValueKind is JsonValueKind.True;

    private static JsonElement? ValueOf(JsonElement? res) =>
        res is { ValueKind: JsonValueKind.Object } o && o.TryGetProperty("value", out var v) ? v : null;

    private void NoteCallFailure(string? label, string text)
    {
        var message = $"UberSDR {label ?? "control"} call failed: {text}";
        CoreLog.Logger.LogWarning("{Message}", message);
        if (label == "tune") _lastTuneError = message;
        else if (label == "mode") _lastModeError = message;
        else _lastPageError = message;
    }

    private static string ErrorText(string label, JsonElement? res)
    {
        var err = res is { ValueKind: JsonValueKind.Object } o && o.TryGetProperty("error", out var e) && e.ValueKind == JsonValueKind.Object ? e : default;
        var code = err.ValueKind == JsonValueKind.Object && err.TryGetProperty("code", out var c) && c.ValueKind == JsonValueKind.String ? c.GetString() : "failed";
        var message = err.ValueKind == JsonValueKind.Object && err.TryGetProperty("message", out var m) && m.ValueKind == JsonValueKind.String ? m.GetString() : "no reason given";
        // The reason is the receiver's own words, which is the whole
        // advantage of a documented API over a readback: "frequency
        // 40000000 is outside 10000-30000000" says what to fix.
        return $"UberSDR refused the {label} ({code}): {message}";
    }

    private string? CombinedError()
    {
        var parts = new[] { _lastAttachError, _lastPageError, _lastTuneError, _lastModeError }
            .Where(e => !string.IsNullOrEmpty(e));
        var joined = string.Join(" | ", parts);
        return joined.Length == 0 ? null : joined;
    }

    private void OnConsole(ConsoleMessage msg)
    {
        if (msg.Type is "error" or "warning")
        {
            CoreLog.Logger.LogWarning("[UberSDR page console:{Type}] {Text}", msg.Type, msg.Text);
        }
    }

    private void OnPageError(Exception exc)
    {
        _lastPageError = $"Unhandled JS error on UberSDR page: {exc.Message}";
        CoreLog.Logger.LogError("{Message}", _lastPageError);
    }
}
