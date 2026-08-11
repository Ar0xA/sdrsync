using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;

namespace SDRSync.Core;

/// <summary>
/// Shared logger for the Core project, set to a real ILogger by the Gui
/// project's composition root at startup (backed by Serilog there). Core
/// depends only on Microsoft.Extensions.Logging.Abstractions, never on a
/// concrete logging implementation, mirroring how sdrsync/config.py uses
/// Python's stdlib `logging.getLogger(...)` without coupling to how
/// handlers are configured.
/// </summary>
public static class CoreLog
{
    public static ILogger Logger { get; set; } = NullLogger.Instance;
}
