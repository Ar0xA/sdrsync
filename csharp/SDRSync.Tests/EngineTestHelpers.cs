using SDRSync.Core;
using SDRSync.Rig;
using SDRSync.WebSdr;

namespace SDRSync.Tests;

/// <summary>Ported from the various test_engine_*.py files' shared StubWebViewHost -- fails loudly if a test that shouldn't touch a WebView does anyway.</summary>
internal sealed class StubWebViewHost : IWebViewHost
{
    public Task<IWebSdrPage> CreatePageAsync(Action<string>? onDead = null) =>
        throw new InvalidOperationException("not expected to be called in these tests");

    public Task DestroyPageAsync(IWebSdrPage page) =>
        throw new InvalidOperationException("not expected to be called in these tests");
}

/// <summary>Ported from test_engine_rig_timeout.py's NeverConnectsRig.</summary>
internal sealed class NeverConnectsRig : IRigClient
{
    public Task<bool> EnsureConnectedAsync() => Task.FromResult(false);

    public Task<RigState> GetStateAsync() => throw new InvalidOperationException("GetStateAsync should never be called if EnsureConnectedAsync failed");

    public Task CloseAsync() => Task.CompletedTask;

    public Task<bool> SetFreqAsync(int freqHz, double? verifyBudgetS = null) => Task.FromResult(false);

    public Task<bool> SetModeAsync(string modeName, int? passbandHz, double? verifyBudgetS = null) => Task.FromResult(false);
}

/// <summary>Ported from test_engine_rig_timeout.py's AlwaysConnectsRig.</summary>
internal sealed class AlwaysConnectsRig : IRigClient
{
    public int EnsureConnectedCalls { get; private set; }

    public Task<bool> EnsureConnectedAsync()
    {
        EnsureConnectedCalls++;
        return Task.FromResult(true);
    }

    public Task<RigState> GetStateAsync() => Task.FromResult(new RigState(null, null, null, null));

    public Task CloseAsync() => Task.CompletedTask;

    public Task<bool> SetFreqAsync(int freqHz, double? verifyBudgetS = null) => Task.FromResult(true);

    public Task<bool> SetModeAsync(string modeName, int? passbandHz, double? verifyBudgetS = null) => Task.FromResult(true);
}
