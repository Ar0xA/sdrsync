"""setup_logging() installs a size-capped RotatingFileHandler, not the
plain unrotated FileHandler it used to -- see project_brief.md's v8 round
for why (disk-growth risk). Relies on force=True (see logging_setup.py's
comment) to actually reinstall handlers even if the root logger already
has some, which it reliably does under pytest."""
import logging
import logging.handlers

from sdrsync.logging_setup import LOG_BACKUP_COUNT, LOG_MAX_BYTES, setup_logging


def _rotating_file_handlers():
    return [h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)]


def test_setup_logging_installs_a_rotating_file_handler(monkeypatch, tmp_path):
    log_file = tmp_path / "sdrsync.log"
    monkeypatch.setattr("sdrsync.logging_setup.LOG_FILE", log_file)

    setup_logging()

    handlers = _rotating_file_handlers()
    assert len(handlers) == 1
    assert handlers[0].maxBytes == LOG_MAX_BYTES
    assert handlers[0].backupCount == LOG_BACKUP_COUNT
    assert log_file.exists()


def test_setup_logging_is_idempotent_across_repeated_calls(monkeypatch, tmp_path):
    """force=True must actually replace handlers on a second call, not
    accumulate duplicates or leak the previous file handle -- a naive
    basicConfig() call (no force) would silently no-op here instead."""
    log_file = tmp_path / "sdrsync.log"
    monkeypatch.setattr("sdrsync.logging_setup.LOG_FILE", log_file)

    setup_logging()
    setup_logging()

    handlers = _rotating_file_handlers()
    assert len(handlers) == 1
