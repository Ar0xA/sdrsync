namespace SDRSync.Sync;

/// <summary>
/// Failure ladder for one forward-push axis (mode OR frequency), ported
/// from sync/engine.py's _ForwardPushBackoff.
///
/// Deliberately not shared with <see cref="ReversePush"/> despite the
/// similar shape: that class's Target/Attempts fields carry reverse-sync-
/// specific meaning (supersede a stale in-flight ladder, a bounded verify
/// budget, and a give-up that reverts the page to the rig) which forward
/// pushes have none of.
///
/// LastTarget exists only to recognize "the rig actually moved to
/// something new" for the one-immediate-retry grant -- it is the value
/// last *attempted*, not a dedupe latch (the engine's own
/// last-sent-mode-key/last-sent-freq fields remain the only "is the WebSDR
/// up to date" answer).
/// </summary>
internal sealed class ForwardPushBackoff
{
    // Settable (not just get) so tests can backdate/force ladder state
    // directly, mirroring Python's total lack of attribute privacy in its
    // own engine tests (e.g. test_forward_push_backoff_exponent_is_clamped
    // sets .failures directly to probe a specific rung without looping).
    public int Failures { get; internal set; }
    public double NextAttemptAt { get; internal set; }
    public object? LastTarget { get; private set; }

    /// <summary>Called each tick a push for this axis is being considered.</summary>
    public void NoteTarget(object? target)
    {
        if (!Equals(target, LastTarget))
        {
            LastTarget = target;
            if (Failures < SyncEngine.ForwardPushImmediateRetryMaxFailures)
            {
                // A genuinely new value is worth one prompt try -- but see
                // ForwardPushImmediateRetryMaxFailures for why that grant
                // stops once the ladder is deep.
                NextAttemptAt = 0.0;
            }
        }
    }

    public void RecordFailure(double nowMonotonicS)
    {
        Failures++;
        var exponent = Math.Min(Failures - 1, SyncEngine.BackoffExponentCap);
        NextAttemptAt = nowMonotonicS + Math.Min(
            SyncEngine.ForwardPushBackoffBaseS * Math.Pow(2, exponent),
            SyncEngine.ForwardPushBackoffMaxS);
    }

    public void RecordSuccess()
    {
        Failures = 0;
        NextAttemptAt = 0.0;
    }

    public void Reset()
    {
        Failures = 0;
        NextAttemptAt = 0.0;
        LastTarget = null;
    }
}
