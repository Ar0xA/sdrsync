namespace SDRSync.Sync;

/// <summary>
/// Monotonic seconds since system startup, analogous to Python's
/// time.monotonic() (CLOCK_MONOTONIC on Linux, also seconds-since-boot).
///
/// Deliberately NOT seconds-since-this-class-was-first-touched (an
/// earlier version used Stopwatch.GetTimestamp() relative to a
/// class-load-time epoch) -- several engine fields use 0.0 as a sentinel
/// meaning "long ago, never held back by this gate" (e.g.
/// SyncEngine._lastWebsdrWriteAt), which only works if "now" is reliably
/// a large number. A class-load-relative clock starts near zero, so a
/// test (or the app itself, moments after startup) could see
/// NowS() - 0.0 read as "not long enough ago yet" and wrongly withhold a
/// write gate's very first pass -- a real bug this caused and a ported
/// test caught (EngineMuteOnTxTests' rising-edge mute silently not firing).
/// Environment.TickCount64 (ms since OS boot) is large enough in any
/// realistic run for the sentinel to behave as intended, matching
/// time.monotonic()'s actual real-world magnitude.
/// </summary>
internal static class MonotonicClock
{
    public static double NowS() => Environment.TickCount64 / 1000.0;
}
