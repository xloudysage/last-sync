"""Bounded local lifecycle diagnostics. Never pass account data or exception bodies."""
import logging
from logging.handlers import RotatingFileHandler

log = logging.getLogger("lastsync.lifecycle")
log.addHandler(logging.NullHandler())
log.propagate = False


def configure(directory):
    if any(isinstance(h, RotatingFileHandler) for h in log.handlers):
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(directory / "lastsync.log", maxBytes=128 * 1024,
                                      backupCount=2, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
