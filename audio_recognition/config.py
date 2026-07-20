"""Configuration.

Settings live in a single app-owned file, musicguru.conf, in the app's own
directory (the folder containing the audio_recognition package). It's this
application's file and nothing else's -- loaded at startup and edited directly
from the /config page. The file is authoritative: values in it win over any
ambient environment. A legacy .env in the same directory is migrated to
musicguru.conf automatically on first run.
"""
import os
import shutil

# The app's own directory -- config lives here so the whole thing is portable:
# copy the folder anywhere and its musicguru.conf travels with it.
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(_APP_DIR, "musicguru.conf")
_LEGACY_ENV = os.path.join(_APP_DIR, ".env")


def _load_conf(path: str) -> None:
    """Parse KEY=VALUE lines into the environment (the file is authoritative)."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()
    except FileNotFoundError:
        pass


# One-time migration: if there's no conf yet but an old .env is sitting there,
# adopt it so existing setups keep working.
if not os.path.exists(CONFIG_PATH) and os.path.exists(_LEGACY_ENV):
    try:
        shutil.copyfile(_LEGACY_ENV, CONFIG_PATH)
    except OSError:
        pass

_load_conf(CONFIG_PATH)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {os.getenv(name)!r}")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        raise SystemExit(f"{name} must be a float, got {os.getenv(name)!r}")


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


# --- paths ---------------------------------------------------------------
AUDIO_FILE = os.getenv("AR_AUDIO_FILE", "/tmp/recorded_audio.wav")
COVER_ART_FILE = os.getenv("AR_COVER_ART_FILE", "/tmp/cover_art.jpg")
# Default moved out of /var/log: that path needs root and made logging_setup
# raise PermissionError at import time.
LOG_FILE = os.getenv("AR_LOG_FILE", "/var/log/musicguru.log")
LOG_LEVEL = os.getenv("AR_LOG_LEVEL", "INFO").upper()
LOG_BACKUP_DAYS = _env_int("AR_LOG_BACKUP_DAYS", 14)

# --- capture -------------------------------------------------------------
ALSA_DEVICE = os.getenv("AR_ALSA_DEVICE", "hw:1,0")
# Capture channels. A room mic is mono (1); a line-in / USB audio capture is
# usually stereo (2) and some such devices refuse mono capture outright. Stereo
# is downmixed to mono after recording, since recognition wants mono anyway.
CAPTURE_CHANNELS = _env_int("AR_CAPTURE_CHANNELS", 1)
RECORD_DURATION = _env_int("AR_RECORD_DURATION", 6)
SAMPLE_RATE = _env_int("AR_SAMPLE_RATE", 44100)
SILENCE_THRESHOLD_DB = _env_float("AR_SILENCE_THRESHOLD_DB", -45.0)
# Peak headroom left by normalize(). The post-normalize boost is clamped so the
# peak can never be pushed above -PEAK_MARGIN_DB, which is what used to clip.
NORMALIZATION_HEADROOM_DB = _env_float("AR_NORMALIZATION_HEADROOM_DB", 3.0)
PEAK_MARGIN_DB = _env_float("AR_PEAK_MARGIN_DB", 1.0)
DESIRED_AVG_DBFS = _env_float("AR_DESIRED_AVG_DBFS", -18.0)
QUIET_BACKOFF_SEC = _env_float("AR_QUIET_BACKOFF_SEC", 1.0)
POLL_INTERVAL_SEC = _env_float("AR_POLL_INTERVAL_SEC", 1.0)
# Consecutive silent segments before Now Playing clears and the EMA resets,
# so the same song replaying after a real gap is logged as a new play.
SILENCE_RESET_SEGMENTS = _env_int("AR_SILENCE_RESET_SEGMENTS", 5)
# Recognition-rate accounting: a stretch of unidentifiable audio only counts
# as 'unmatched' once it runs this many consecutive segments. Short gaps
# between or within recognized songs don't count against the match rate.
UNMATCHED_MIN_SEGMENTS = _env_int("AR_UNMATCHED_MIN_SEGMENTS", 10)

# --- recognition ---------------------------------------------------------
SHAZAM_TIMEOUT = _env_float("AR_SHAZAM_TIMEOUT", 10.0)
EMA_ALPHA = _env_float("AR_EMA_ALPHA", 0.5)
EMA_THRESHOLD = _env_float("AR_EMA_THRESHOLD", 0.7)
EMA_PRUNE_EPSILON = _env_float("AR_EMA_PRUNE_EPSILON", 1e-3)

# --- enrichment ----------------------------------------------------------
# Shazam rarely returns duration and often omits album/genre. When enabled, a
# confirmed track is looked up (Last.fm, same source as backfill_metadata.py)
# to fill those columns before it is displayed and saved -- which is what makes
# the Now Playing progress bar work at all, since it divides by duration.
# Requires AR_LASTFM_API_KEY; without it, enrichment is a no-op.
ENRICH_ENABLED = _env_bool("AR_ENRICH", True)
# Last.fm's duration field is unreliable. When it comes back empty, fall back to
# MusicBrainz (no key required, throttled to 1 req/sec) purely for duration.
ENRICH_MUSICBRAINZ = _env_bool("AR_ENRICH_MUSICBRAINZ", True)
MUSICBRAINZ_TIMEOUT = _env_float("AR_MUSICBRAINZ_TIMEOUT", 6.0)
# MusicBrainz blocks generic/empty User-Agents. Adding a contact URL or email is
# their requested etiquette, e.g. "audio_recognition/1.0 ( you@example.com )".
MUSICBRAINZ_USER_AGENT = os.getenv("AR_MUSICBRAINZ_UA", "audio_recognition/1.0")

# --- display -------------------------------------------------------------
DISPLAY_ENABLED = _env_bool("AR_DISPLAY_ENABLED", True)
DISPLAY_SIZE = (_env_int("AR_DISPLAY_W", 800), _env_int("AR_DISPLAY_H", 480))
FONT_PATH = os.getenv(
    "AR_FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
)
FONT_SIZE = _env_int("AR_FONT_SIZE", 40)
IMAGE_RETRIES = _env_int("AR_IMAGE_RETRIES", 3)
IMAGE_TIMEOUT = _env_float("AR_IMAGE_TIMEOUT", 5.0)
IMAGE_MAX_BYTES = _env_int("AR_IMAGE_MAX_BYTES", 8 * 1024 * 1024)
FEH_RELOAD_SEC = _env_int("AR_FEH_RELOAD_SEC", 1)

# --- cover cache ---------------------------------------------------------
# Shazam's CDN links rot. Every cover is copied here once and served locally
# from /cover/<id> thereafter.
COVER_CACHE_ENABLED = _env_bool("AR_COVER_CACHE", True)
COVER_CACHE_DIR = os.getenv(
    "AR_COVER_CACHE_DIR", os.path.expanduser("~/.cache/audio_recognition/covers")
)
COVER_MAX_PX = _env_int("AR_COVER_MAX_PX", 500)
# Also store the re-encoded cover bytes IN the database, so the art survives a
# lost disk cache and never has to be re-fetched from the internet. The disk
# cache stays as a fast front layer; the DB is the durable source of truth.
COVER_DB_ENABLED = _env_bool("AR_COVER_DB", True)

# --- local recognition (fingerprint cache) -------------------------------
# Identify a track that has been heard before WITHOUT calling Shazam, by
# matching a Chromaprint fingerprint of the segment against ones stored from
# past identifications. Requires the `fpcalc` binary (Debian/Ubuntu:
# `apt install libchromaprint-tools`). If fpcalc is missing this silently
# stays off and every segment goes to Shazam as before.
LOCAL_RECOGNITION = _env_bool("AR_LOCAL_RECOGNITION", True)
# Similarity in [0,1] required to accept a local match. Higher = fewer false
# hits but more Shazam fallbacks. Same audio scores ~0.9+, unrelated ~0.5.
FP_MATCH_THRESHOLD = _env_float("AR_FP_MATCH_THRESHOLD", 0.78)
# How many reference fingerprints to keep per track. Different parts of a song
# fingerprint differently, so several references raise the local hit rate.
FP_MAX_PER_TRACK = _env_int("AR_FP_MAX_PER_TRACK", 8)
# Seconds of audio fpcalc hashes from each segment.
FP_LENGTH_SEC = _env_int("AR_FP_LENGTH_SEC", 10)

# --- database ------------------------------------------------------------
DB_CONFIG = {
    "host": os.getenv("AR_DB_HOST", "localhost"),
    "port": _env_int("AR_DB_PORT", 3306),
    "user": os.getenv("AR_DB_USER", "musicuser"),
    "password": os.getenv("AR_DB_PASSWORD"),  # required, no default
    "database": os.getenv("AR_DB_NAME", "music_log"),
}
DB_POOL_SIZE = _env_int("AR_DB_POOL_SIZE", 5)
# recognized_at is written with UTC_TIMESTAMP() by save_track(), so rows are
# unambiguously UTC regardless of the MySQL server's time_zone setting.
# Set to 0 only if you have legacy rows written in server-local time.
DB_TIMES_ARE_UTC = _env_bool("AR_DB_TIMES_ARE_UTC", True)

# --- plex ----------------------------------------------------------------
PLEX_BASE_URL = os.getenv("AR_PLEX_BASE_URL", "").rstrip("/")
PLEX_TOKEN = os.getenv("AR_PLEX_TOKEN")  # required for playlists, no default
PLEX_TIMEOUT = _env_float("AR_PLEX_TIMEOUT", 8.0)
PLEX_MUSIC_TYPE = 10  # Plex metadata type id for "track"
# Verify TLS. Turn OFF when pointing at https://<ip>:32400, since Plex's
# *.plex.direct certificate won't validate against a bare IP.
PLEX_VERIFY_SSL = _env_bool("AR_PLEX_VERIFY_SSL", True)
# Explicit music library name. Leave unset to auto-pick the first music section.
PLEX_MUSIC_SECTION = os.getenv("AR_PLEX_MUSIC_SECTION")
# The want-list checks hundreds of your recognized tracks against Plex. Each is
# a fast indexed title search server-side, but they're run concurrently so the
# page loads in seconds rather than minutes. Raise/lower to taste.
PLEX_CONCURRENCY = _env_int("AR_PLEX_CONCURRENCY", 12)

# --- Local music library (works without Plex) ----------------------------
# If you don't run Plex, point this at a folder of music files. It's scanned
# once (tags read with mutagen) into the same title->artist index Plex uses, so
# the want-list and the "in library" badge work, and playlists export as an M3U
# of the real file paths (portable, plays in any player). Plex, if configured,
# takes precedence; this is the fallback.
LOCAL_LIBRARY_PATH = os.getenv("AR_LOCAL_LIBRARY_PATH")
LOCAL_LIBRARY_EXTS = os.getenv(
    "AR_LOCAL_LIBRARY_EXTS", ".mp3,.flac,.m4a,.ogg,.opus,.wav,.aac,.wma")

# --- Spotify (playlist export) -------------------------------------------
# Create a Spotify playlist from selected/recognized tracks. Requires a free
# Spotify developer app (https://developer.spotify.com/dashboard): create one,
# add a redirect URI that points back at this console, and set the three values
# below. Then click "Connect Spotify" on the /config page once to authorize.
SPOTIFY_CLIENT_ID = os.getenv("AR_SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("AR_SPOTIFY_CLIENT_SECRET")
# Must EXACTLY match a redirect URI registered in the Spotify app, e.g.
# http://192.168.1.50:8000/spotify/callback
SPOTIFY_REDIRECT_URI = os.getenv("AR_SPOTIFY_REDIRECT_URI")
SPOTIFY_TOKEN_CACHE = os.getenv(
    "AR_SPOTIFY_TOKEN_CACHE",
    os.path.join(_APP_DIR, "spotify-session.json"),
)
SPOTIFY_PLAYLIST_PUBLIC = _env_bool("AR_SPOTIFY_PLAYLIST_PUBLIC", False)

# --- Tidal (playlist export) ---------------------------------------------
# No developer app needed -- Tidal uses a one-time device login. Enable it, then
# authorize once from the Pi:  python -m audio_recognition.services.tidal login
# (it prints a link.tidal.com URL to approve). The session is cached and
# refreshed automatically after that.
TIDAL_ENABLED = _env_bool("AR_TIDAL_ENABLED", False)
# Stored next to the app (not under ~/.config) so it persists across restarts
# regardless of the service's HOME -- that HOME mismatch is what forced repeated
# re-auth. Override with AR_TIDAL_TOKEN_CACHE if you want it elsewhere.
TIDAL_TOKEN_CACHE = os.getenv(
    "AR_TIDAL_TOKEN_CACHE",
    os.path.join(_APP_DIR, "tidal-session.json"),
)

# --- auto-playlist -------------------------------------------------------
# Continuously append every newly heard track to a playlist on each enabled
# service. One shared playlist name; toggle per service. Tracks are de-duplicated
# (each distinct song added once per service), and the dedupe survives restarts.
AUTO_PLAYLIST_NAME = os.getenv("AR_AUTO_PLAYLIST_NAME", "musicguru - heard")
AUTO_PLAYLIST_SPOTIFY = _env_bool("AR_AUTO_PLAYLIST_SPOTIFY", False)
AUTO_PLAYLIST_TIDAL = _env_bool("AR_AUTO_PLAYLIST_TIDAL", False)
AUTO_PLAYLIST_PLEX = _env_bool("AR_AUTO_PLAYLIST_PLEX", False)
# When False (the default), generated playlists point at this app's /stream/<id>
# proxy instead of embedding X-Plex-Token in a file you hand to other people.
PLAYLIST_EMBED_TOKEN = _env_bool("AR_PLAYLIST_EMBED_TOKEN", False)

# --- last.fm -------------------------------------------------------------
LASTFM_API_KEY = os.getenv("AR_LASTFM_API_KEY")
LASTFM_TIMEOUT = _env_float("AR_LASTFM_TIMEOUT", 5.0)
# LASTFM_SHARED_SECRET removed: it is only needed for signed/authenticated
# calls (scrobbling), which this app does not make.

# --- web -----------------------------------------------------------------
# Was 0.0.0.0 with no auth. Loopback by default; put a reverse proxy in front
# if you want it on the LAN, or set AR_WEB_TOKEN.
FLASK_HOST = os.getenv("AR_FLASK_HOST", "127.0.0.1")
FLASK_PORT = _env_int("AR_FLASK_PORT", 8000)
FLASK_DEBUG = _env_bool("AR_FLASK_DEBUG", False)
WEB_TOKEN = os.getenv("AR_WEB_TOKEN")  # optional shared secret for API/machine access
ARCHIVE_MAX_LIMIT = _env_int("AR_ARCHIVE_MAX_LIMIT", 100)

# --- web login (optional) ------------------------------------------------
# Interactive username/password login for the console. Set a password (hashed
# preferred) to require a login form; sessions are signed cookies. AR_WEB_TOKEN
# still works alongside this for API/scraper access (Prometheus, healthz).
# Generate a hash with:  python -m audio_recognition.webapp.auth 'your password'
WEB_USER = os.getenv("AR_WEB_USER", "admin")
WEB_PASSWORD = os.getenv("AR_WEB_PASSWORD")           # plaintext (optional)
WEB_PASSWORD_HASH = os.getenv("AR_WEB_PASSWORD_HASH")  # preferred (optional)
# Signs the session cookie. If unset, a stable key is derived from the password
# so logins survive a restart; set this explicitly to rotate all sessions.
WEB_SECRET_KEY = os.getenv("AR_WEB_SECRET_KEY")
# Minutes a login stays valid. 0 = until the browser closes.
WEB_SESSION_HOURS = _env_int("AR_WEB_SESSION_HOURS", 720)

# --- config editor -------------------------------------------------------
# The /config page always edits musicguru.conf (CONFIG_PATH) in place; a login
# is mandatory (set up on first run), so the editor is protected. Edits take
# effect on the next restart, except the account credentials, which apply live.


# --- scrobbling (optional) ----------------------------------------------
# Scrobble confirmed plays to Last.fm. Needs the API key above PLUS a shared
# secret and a one-time session key (see scrobble.get_session_key). No-op unless
# AR_SCROBBLE is on and all three are present.
SCROBBLE_ENABLED = _env_bool("AR_SCROBBLE", False)
LASTFM_SECRET = os.getenv("AR_LASTFM_SECRET")
LASTFM_SESSION_KEY = os.getenv("AR_LASTFM_SESSION_KEY")
# Below this many seconds a play is never scrobbled (Last.fm's own floor is 30s).
SCROBBLE_MIN_SECONDS = _env_int("AR_SCROBBLE_MIN_SECONDS", 30)

# --- now-playing publishing (optional) ----------------------------------
# POST the current track as JSON to a webhook (e.g. a Home Assistant webhook
# trigger) on every change. Empty disables it.
NOWPLAYING_WEBHOOK = os.getenv("AR_NOWPLAYING_WEBHOOK", "").strip()
# Optional MQTT, only used if paho-mqtt is installed and a host is set.
MQTT_HOST = os.getenv("AR_MQTT_HOST", "").strip()
MQTT_PORT = _env_int("AR_MQTT_PORT", 1883)
MQTT_TOPIC = os.getenv("AR_MQTT_TOPIC", "audio_recognition/now_playing")
MQTT_USER = os.getenv("AR_MQTT_USER")
MQTT_PASSWORD = os.getenv("AR_MQTT_PASSWORD")
PUBLISH_TIMEOUT = _env_float("AR_PUBLISH_TIMEOUT", 4.0)

# --- watchdog / alerts (optional) ---------------------------------------
# If no audio signal is seen for this many minutes during active hours, fire a
# one-shot notification. 0 disables. Active hours are local [lo, hi).
WATCHDOG_SILENCE_MIN = _env_int("AR_WATCHDOG_SILENCE_MIN", 0)
ACTIVE_HOURS = os.getenv("AR_ACTIVE_HOURS", "0-24")  # e.g. "8-23"
# Notification sink: a webhook receiving {"title","message"} as JSON, or an ntfy
# topic URL (plain-text body). Empty = log only.
NOTIFY_URL = os.getenv("AR_NOTIFY_URL", "").strip()
NOTIFY_TIMEOUT = _env_float("AR_NOTIFY_TIMEOUT", 4.0)

# --- durability ----------------------------------------------------------
# When a DB insert fails, plays are appended here as JSONL and replayed on the
# next successful write, so a MySQL blip doesn't punch holes in the history.
DB_SPOOL_FILE = os.getenv(
    "AR_DB_SPOOL_FILE", os.path.expanduser("~/.local/state/audio_recognition_spool.jsonl")
)


class ConfigError(RuntimeError):
    pass


def validate(require_plex: bool = False, require_lastfm: bool = False) -> None:
    """Fail fast at startup instead of at first use."""
    missing = []
    if not DB_CONFIG["password"]:
        missing.append("AR_DB_PASSWORD")
    if require_plex and (not PLEX_BASE_URL or not PLEX_TOKEN):
        missing.append("AR_PLEX_BASE_URL / AR_PLEX_TOKEN")
    if require_lastfm and not LASTFM_API_KEY:
        missing.append("AR_LASTFM_API_KEY")
    if missing:
        raise ConfigError("Missing required environment variables: " + ", ".join(missing))
    if not 0.0 < EMA_ALPHA < 1.0:
        raise ConfigError("AR_EMA_ALPHA must be strictly between 0 and 1")
