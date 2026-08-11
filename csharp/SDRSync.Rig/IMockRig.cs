namespace SDRSync.Rig;

/// <summary>
/// Common shape the sync engine needs from an embedded mock rig server
/// (FakeRigctldServer or FakeFlrigServer). Ported from the duck-typed
/// close()/await wait_closed() contract sync/engine.py's Python original
/// relies on for its "_mock_server: asyncio.AbstractServer |
/// FlrigMockServerHandle" field -- C#'s nominal typing needs an explicit
/// interface for the same structural relationship.
/// </summary>
public interface IMockRigServer
{
    void Close();

    Task WaitClosedAsync();
}

/// <summary>
/// Common shape the sync engine needs from an embedded mock rig's mutable
/// state (FakeRigctldState or FakeFlrigState), for the GUI-driven mock-rig
/// control panel's PushMockFreq/PushMockMode/PushMockPtt.
/// </summary>
public interface IMockRigState
{
    int FreqHz { get; set; }

    string Mode { get; set; }

    int PassbandHz { get; set; }

    string Ptt { get; set; }
}
