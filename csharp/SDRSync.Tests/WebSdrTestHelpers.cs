using System.Text.Json;
using SDRSync.WebSdr;
using Xunit;

namespace SDRSync.Tests;

/// <summary>
/// xUnit runs different test classes in parallel by default, but only
/// serializes methods WITHIN one class -- WebSDRDriverRegistry.Drivers is
/// a shared static dictionary (SDRSync.WebSdr/IWebSDRDriver.cs), so any
/// two test classes that both write to it need to be pinned into the same
/// collection or they can race on each other's entries. Both
/// EngineSwitchSiteTests (writes stub factories under "websdr_org"/
/// "kiwisdr") and WebSdrRegistryTests (writes the real factories via
/// RegisterBuiltinDrivers()) opt into this collection for exactly that
/// reason -- confirmed necessary live: WebSdrRegistryTests' registration
/// test intermittently clobbered EngineSwitchSiteTests' stub with the real
/// WebsdrOrgDriver factory before this was added, causing an
/// InvalidCastException in an unrelated test.
/// </summary>
[CollectionDefinition("WebSDRDriverRegistry")]
public class WebSdrDriverRegistryCollection
{
}

/// <summary>Base IWebSdrPage stub with safe no-op defaults for every member -- WebSDR driver tests (websdr_org/kiwisdr/openwebrx/ubersdr) override only the handful of members each test actually needs, matching the Python originals' own lightweight StubPage classes.</summary>
internal class StubPageBase : IWebSdrPage
{
    public virtual Task NavigateAsync(string url, double timeoutS) => Task.CompletedTask;
    public virtual Task<JsonElement?> EvaluateAsync(string js, params object?[] args) => Task.FromResult<JsonElement?>(null);
    public virtual Task WaitForFunctionAsync(string js, double timeoutS) => Task.CompletedTask;
    public virtual Task ClickAsync(int x, int y) => Task.CompletedTask;
    public virtual void OnConsole(Action<ConsoleMessage> handler) { }
    public virtual void OnPageError(Action<Exception> handler) { }
    public virtual void SetOnDead(Action<string> onDead) { }
    public virtual Task CloseAsync() => Task.CompletedTask;
}
