namespace SDRSync.Core;

/// <summary>
/// Base type for messages published on the GUI's status channel, ported
/// from sdrsync/gui_messages.py. SyncEngine (StatusSnapshot), preflight
/// checks, and other background producers publish subclasses of this onto
/// one shared System.Threading.Channels.Channel&lt;GuiMessage&gt;; the GUI
/// dispatches on exact runtime type via a lookup table rather than a
/// hand-rolled type-check chain that would keep growing as more message
/// types get added. Lives in Core (not Sync/WebSdr) so neither of those
/// projects has to reference the other just to share this base.
/// </summary>
public abstract record GuiMessage;
