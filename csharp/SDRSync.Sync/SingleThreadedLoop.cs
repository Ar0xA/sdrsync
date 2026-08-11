using System.Collections.Concurrent;

namespace SDRSync.Sync;

/// <summary>
/// A dedicated background thread running its own message pump, giving the
/// sync engine the same cooperative-single-threaded-execution guarantee
/// Python's asyncio event loop provides. This is foundational to every
/// "generation counter checked immediately after an await" correctness
/// argument ported from sync/engine.py: two logical tasks (the poll loop
/// tick, the attach supervisor) can interleave at await points, but never
/// truly run in parallel, so a state check immediately after resuming from
/// an await is safe without a lock -- exactly like the Python original's
/// reliance on asyncio's single-threaded event loop.
///
/// Considered Nito.AsyncEx.Context (the well-known NuGet package for
/// exactly this "async-compatible single-threaded context" pattern), but
/// its last release was 2021-09 -- given the explicit instruction to avoid
/// stale dependencies, and that this is a small, stable, well-documented
/// .NET idiom (a custom SynchronizationContext plus a dedicated pump
/// thread -- the same mechanism WPF/WinForms use for UI-thread affinity),
/// hand-rolling it here is the better call than depending on an abandoned
/// package for it.
/// </summary>
public sealed class SingleThreadedLoop : IDisposable
{
    private readonly BlockingCollection<(SendOrPostCallback Callback, object? State)> _queue = new();
    private readonly Thread _thread;
    private readonly EngineSyncContext _syncContext;
    private volatile bool _stopping;

    public SingleThreadedLoop(string threadName)
    {
        _syncContext = new EngineSyncContext(this);
        _thread = new Thread(RunPump) { IsBackground = true, Name = threadName };
        _thread.Start();
    }

    private void RunPump()
    {
        SynchronizationContext.SetSynchronizationContext(_syncContext);
        try
        {
            foreach (var (callback, state) in _queue.GetConsumingEnumerable())
            {
                callback(state);
            }
        }
        catch (Exception)
        {
            // A callback that throws synchronously (not via an awaited
            // Task) would otherwise kill the pump thread silently -- the
            // engine's own methods are responsible for catching/logging
            // their own exceptions (mirroring _poll_loop()'s own
            // try/except around _tick()); this is a last-resort guard so a
            // bug elsewhere doesn't leave the whole loop permanently dead
            // with no diagnostic.
        }
    }

    private void PostRaw(SendOrPostCallback callback, object? state)
    {
        if (_stopping) return;
        try
        {
            _queue.Add((callback, state));
        }
        catch (InvalidOperationException)
        {
            // CompleteAdding() already called -- loop is shutting down.
        }
    }

    /// <summary>
    /// Schedules an async entry point to run on this loop's thread and
    /// returns immediately -- fire-and-forget, mirroring
    /// asyncio.ensure_future()/run_coroutine_threadsafe() used throughout
    /// the Python original's *_from_other_thread() methods and the attach
    /// supervisor's own launch.
    /// </summary>
    public void Post(Func<Task> asyncAction)
    {
        PostRaw(_ => _ = RunAndObserve(asyncAction), null);
    }

    private static async Task RunAndObserve(Func<Task> asyncAction)
    {
        await asyncAction();
    }

    /// <summary>
    /// Like <see cref="Post(Func{Task})"/>, but returns a Task the caller
    /// can await/cancel-and-await -- mirrors asyncio.ensure_future()'s
    /// returned Task, used by the attach supervisor so
    /// StopWebsdrAsync/SwitchWebsdrAsync can cancel it and wait for it to
    /// actually finish before proceeding.
    /// </summary>
    public Task PostAndTrack(Func<Task> asyncAction)
    {
        var tcs = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        PostRaw(_ => _ = RunTrackedAsync(asyncAction, tcs), null);
        return tcs.Task;
    }

    private static async Task RunTrackedAsync(Func<Task> asyncAction, TaskCompletionSource tcs)
    {
        try
        {
            await asyncAction();
            tcs.TrySetResult();
        }
        catch (OperationCanceledException)
        {
            tcs.TrySetCanceled();
        }
        catch (Exception e)
        {
            tcs.TrySetException(e);
        }
    }

    /// <summary>
    /// Runs the given async entry point on this loop's thread. Used once,
    /// to actually start the engine (mirrors `asyncio.run(engine.run())`
    /// running on its own dedicated background thread in the Python
    /// original's app.py).
    /// </summary>
    public Task RunAsync(Func<Task> asyncMain)
    {
        var tcs = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        PostRaw(_ => _ = RunMainAsync(asyncMain, tcs), null);
        return tcs.Task;
    }

    private static async Task RunMainAsync(Func<Task> asyncMain, TaskCompletionSource tcs)
    {
        try
        {
            await asyncMain();
            tcs.TrySetResult();
        }
        catch (Exception e)
        {
            tcs.TrySetException(e);
        }
    }

    public void Stop()
    {
        _stopping = true;
        _queue.CompleteAdding();
    }

    public void Dispose()
    {
        Stop();
        _thread.Join(TimeSpan.FromSeconds(5));
        _queue.Dispose();
    }

    private sealed class EngineSyncContext(SingleThreadedLoop loop) : SynchronizationContext
    {
        public override void Post(SendOrPostCallback d, object? state) => loop.PostRaw(d, state);

        public override void Send(SendOrPostCallback d, object? state) =>
            throw new NotSupportedException("Synchronous Send is not supported on SingleThreadedLoop -- would deadlock against its own pump.");

        public override SynchronizationContext CreateCopy() => this;
    }
}
