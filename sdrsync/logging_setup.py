"""Structured logging shared by every module (console + persistent log file)."""
import logging
import logging.handlers
from pathlib import Path

LOG_FILE = Path.home() / ".sdrsync" / "sdrsync.log"

# Size-based, not time-based (TimedRotatingFileHandler) -- log volume here
# tracks usage sessions, not calendar time, so a size cap gives predictable
# disk usage regardless of how often the app is run.
LOG_MAX_BYTES = 5_000_000
LOG_BACKUP_COUNT = 3


def setup_logging(level: int = logging.INFO) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.handlers.RotatingFileHandler(
                LOG_FILE, mode="a", encoding="utf-8",
                maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
            ),
        ],
        # basicConfig() is a no-op if the root logger already has handlers
        # (e.g. a test runner's own logging plugin, or a second
        # setup_logging() call in the same process) -- force=True makes it
        # actually (re)install ours instead of silently doing nothing.
        force=True,
    )
    # Known limitation, not fixed: on Windows, a second process holding
    # sdrsync.log open (e.g. two SDRSync instances) makes the rotate-time
    # rename fail; logging's own handleError() swallows that rather than
    # crashing, so the cap silently stops being enforced for whichever
    # instance lost the race. Not a supported configuration, so left as-is.
