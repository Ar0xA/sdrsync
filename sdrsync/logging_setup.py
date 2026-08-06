"""Structured logging shared by every module (console + persistent log file)."""
import logging
from pathlib import Path

LOG_FILE = Path.home() / ".sdrsync" / "sdrsync.log"


def setup_logging(level: int = logging.INFO) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8"),
        ],
    )
