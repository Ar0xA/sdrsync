"""Base type for messages published on the GUI's status_queue.

Both SyncEngine (StatusSnapshot) and preflight.py (PreflightResult,
DetectResult) publish instances of subclasses of this onto the same
queue.Queue; gui/app.py dispatches on exact type via a lookup table rather
than a hand-rolled isinstance chain that would keep growing as more
message types get added. Lives in its own tiny module (rather than in
engine.py or preflight.py) so neither has to import the other just to
share this base.
"""
from dataclasses import dataclass


@dataclass
class GuiMessage:
    pass
