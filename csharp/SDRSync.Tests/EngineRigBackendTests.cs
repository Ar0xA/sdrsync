using SDRSync.Core;
using SDRSync.Rig;
using SDRSync.Sync;
using Xunit;

namespace SDRSync.Tests;

/// <summary>
/// Ported from tests/test_engine_rig_backend.py. StartRigAsync() must
/// construct the right client class for the selected backend, and
/// switching backends must tear down the previous rig client cleanly.
/// Neither RigctldClient nor FlrigClient connects anything at
/// construction time, so StartRigAsync(useMock: false) can be exercised
/// directly without hitting the network.
/// </summary>
public class EngineRigBackendTests
{
    private static SyncEngine MakeEngine() => new(new AppSettings(), new StubWebViewHost());

    [Fact]
    public async Task StartRig_RigctldBackend_ConstructsRigctldClient()
    {
        var engine = MakeEngine();

        await engine.StartRigAsync("rigctld", "127.0.0.1", 4532, false);

        Assert.IsType<RigctldClient>(engine._rig);
        Assert.True(engine._rigActive);
    }

    [Fact]
    public async Task StartRig_FlrigBackend_ConstructsFlrigClient()
    {
        var engine = MakeEngine();

        await engine.StartRigAsync("flrig", "127.0.0.1", 12345, false);

        Assert.IsType<FlrigClient>(engine._rig);
        Assert.True(engine._rigActive);
    }

    [Fact]
    public async Task SwitchingBackend_ReplacesTheClient()
    {
        var engine = MakeEngine();

        await engine.StartRigAsync("rigctld", "127.0.0.1", 4532, false);
        var firstRig = engine._rig;
        Assert.IsType<RigctldClient>(firstRig);

        await engine.StartRigAsync("flrig", "127.0.0.1", 12345, false);

        Assert.IsType<FlrigClient>(engine._rig);
        Assert.NotSame(firstRig, engine._rig);
    }
}
