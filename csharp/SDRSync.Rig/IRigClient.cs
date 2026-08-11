namespace SDRSync.Rig;

/// <summary>
/// The narrow interface the sync engine needs from either rig backend,
/// ported from sync/engine.py's local RigClient Protocol. Both
/// RigctldClient and FlrigClient implement this explicitly -- a deliberate
/// difference from the Python original, where a Protocol is satisfied
/// structurally with no declaration needed on the implementing class.
/// C#'s nominal interfaces need that declaration, so it's placed here
/// (next to the two concrete clients) rather than in SDRSync.Sync, since a
/// dependency from SDRSync.Rig back to SDRSync.Sync (to reference an
/// interface defined there) would invert the project reference graph.
/// ReconnectDelay() exists on both concrete clients but, like the Python
/// original, has no call site from the engine, so it's intentionally
/// excluded here.
/// </summary>
public interface IRigClient
{
    Task<bool> EnsureConnectedAsync();

    Task<RigState> GetStateAsync();

    Task CloseAsync();

    Task<bool> SetFreqAsync(int freqHz, double? verifyBudgetS = null);

    Task<bool> SetModeAsync(string modeName, int? passbandHz, double? verifyBudgetS = null);
}
