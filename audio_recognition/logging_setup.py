import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

from .config import LOG_FILE, LOG_LEVEL, LOG_BACKUP_DAYS, _APP_DIR

_configured = False


def _open_file_handler(fmt):
    """Attach a daily-rotating file handler at the first writable candidate:
    the configured path (default /var/log/musicguru.log), then next to the app,
    then the user state dir. Returns the handler or None."""
    candidates = [LOG_FILE,
                  os.path.join(_APP_DIR, "musicguru.log"),
                  os.path.expanduser("~/.local/state/musicguru.log")]
    last_err = None
    for path in candidates:
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            fh = TimedRotatingFileHandler(
                path, when="D", interval=1, backupCount=LOG_BACKUP_DAYS)
            fh.setFormatter(fmt)
            return fh, path, None
        except OSError as e:
            last_err = e
            continue
    return None, None, last_err


def setup_logging(level=None):
    """Configure the 'audio_recognition' logger. Safe to call more than once."""
    global _configured
    log = logging.getLogger("audio_recognition")
    if _configured:
        return log

    level = level or getattr(logging, LOG_LEVEL, logging.INFO)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    handlers = [console]

    fh, path, err = _open_file_handler(fmt)
    if fh is not None:
        handlers.append(fh)
        if path != LOG_FILE:
            console.handle(logging.LogRecord(
                "audio_recognition", logging.WARNING, __file__, 0,
                "Logging to %s (couldn't use %s)", (path, LOG_FILE), None))
    else:
        console.handle(logging.LogRecord(
            "audio_recognition", logging.WARNING, __file__, 0,
            "File logging disabled: %s", (err,), None))

    log.setLevel(level)
    log.propagate = False
    for h in handlers:
        log.addHandler(h)

    _configured = True
    return log
