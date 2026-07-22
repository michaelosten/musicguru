"""Logging setup: readable, aligned, symbol-tagged lines.

The console gets colour (when it's a TTY); the log FILE never does -- ANSI escape
codes would wreck grep/less on /var/log/musicguru.log. Both share the same layout
so a line looks the same either way, minus the colour.

Layout:
    HH:MM:SS  [+] plex      added TOOL - Schism
              ^   ^         ^
              |   |         message
              |   source (module, padded)
              status symbol
"""
import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

from .config import LOG_FILE, LOG_LEVEL, LOG_BACKUP_DAYS, _APP_DIR
from . import __version__

_configured = False

# ---- palette -------------------------------------------------------------
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
_C = {
    "grey": "\033[90m", "red": "\033[31m", "green": "\033[32m",
    "yellow": "\033[33m", "blue": "\033[34m", "magenta": "\033[35m",
    "cyan": "\033[36m", "white": "\033[37m",
}

# level -> (symbol, colour)
_LEVELS = {
    logging.DEBUG:    ("...", "grey"),
    logging.INFO:     ("[+]", "green"),
    logging.WARNING:  ("[!]", "yellow"),
    logging.ERROR:    ("[x]", "red"),
    logging.CRITICAL: ("[X]", "magenta"),
}

# Message-content cues that deserve their own symbol, so a glance tells you what
# happened without reading the text.
_CUES = (
    ("not found", ("[-]", "grey")),
    ("already in", ("[=]", "grey")),
    ("skipping", ("[-]", "grey")),
    ("unavailable", ("[~]", "yellow")),
    ("will retry", ("[~]", "yellow")),
    ("backing off", ("[~]", "yellow")),
    ("added", ("[+]", "green")),
    ("no match", ("[-]", "grey")),
    ("fuzzy match", ("[?]", "cyan")),
    ("now playing", ("[>]", "cyan")),
)


def _short_source(name: str) -> str:
    """'audio_recognition.services.tidal' -> 'tidal'."""
    return (name or "").split(".")[-1][:9]


def _colour_enabled(stream) -> bool:
    mode = os.getenv("AR_LOG_COLOR", "auto").strip().lower()
    if mode in ("0", "never", "off", "no"):
        return False
    if mode in ("1", "always", "on", "yes"):
        return True
    if os.getenv("NO_COLOR"):
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


class _Formatter(logging.Formatter):
    """Shared layout; colour is opt-in so the file copy stays plain."""

    def __init__(self, colour: bool):
        super().__init__()
        self.colour = colour

    def _decorate(self, record):
        sym, col = _LEVELS.get(record.levelno, ("[+]", "white"))
        if record.levelno <= logging.INFO:
            msg = str(record.getMessage()).lower()
            for cue, (s, c) in _CUES:
                if cue in msg:
                    sym, col = s, c
                    break
        return sym, col

    def format(self, record):
        sym, col = self._decorate(record)
        ts = self.formatTime(record, "%H:%M:%S")
        src = _short_source(record.name)
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        if not self.colour:
            return f"{ts}  {sym} {src:<9} {msg}"
        c = _C.get(col, "")
        return (f"{DIM}{ts}{RESET}  {c}{sym}{RESET} "
                f"{DIM}{src:<9}{RESET} {c if col in ('red','magenta') else ''}{msg}"
                f"{RESET if col in ('red','magenta') else ''}")


BANNER = r"""
  __  __ _   _ ___ ___ ___  ___ _   _ ___ _   _
 |  \/  | | | / __|_ _/ __|/ __| | | | _ \ | | |
 | |\/| | |_| \__ \| | (__| (_ | |_| |   / |_| |
 |_|  |_|\___/|___/___\___|\___|\___/|_|_\___/
  listening . recognizing . logging      v{ver}
"""


def _emit_banner(handlers, colour_map) -> None:
    """Write the banner straight to each stream, unprefixed -- a log line per
    art row would be unreadable."""
    art = BANNER.replace("{ver}", __version__).strip("\n")
    for h in handlers:
        try:
            stream = getattr(h, "stream", None)
            if stream is None:
                continue
            text = art
            if colour_map.get(id(h)):
                text = f"{_C['cyan']}{BOLD}{art}{RESET}"
            stream.write(text + "\n")
            h.flush()
        except Exception:
            pass


def _open_file_handler(fmt):
    """Attach a daily-rotating file handler at the first writable candidate:
    the configured path (default /var/log/musicguru.log), then next to the app,
    then the user state dir. Returns (handler, path, error)."""
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


def setup_logging(level=None, banner: bool = False):
    """Configure the 'audio_recognition' logger. Safe to call more than once."""
    global _configured
    log = logging.getLogger("audio_recognition")
    if _configured:
        return log

    level = level or getattr(logging, LOG_LEVEL, logging.INFO)
    console_colour = _colour_enabled(sys.stderr)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(_Formatter(colour=console_colour))
    handlers = [console]

    fh, path, err = _open_file_handler(_Formatter(colour=False))
    if fh is not None:
        handlers.append(fh)
    log.setLevel(level)
    log.propagate = False
    for h in handlers:
        log.addHandler(h)

    if fh is None:
        log.warning("File logging disabled: %s", err)
    elif path != LOG_FILE:
        log.warning("Logging to %s (couldn't use %s)", path, LOG_FILE)

    _configured = True
    if banner:
        _emit_banner(handlers, {id(console): console_colour})
    return log


def bar(done: int, total: int, width: int = 24) -> str:
    """ASCII progress bar: '[#########---------------]  38% 2560/6749'."""
    total = max(0, int(total))
    done = max(0, min(int(done), total)) if total else 0
    pct = (done / total * 100) if total else 100.0
    filled = int(round(width * (done / total))) if total else width
    return f"[{'#' * filled}{'-' * (width - filled)}] {pct:3.0f}% {done}/{total}"
