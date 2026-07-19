"""Edit a whitelist of AR_* settings in the .env file from the Config page.

Deliberately conservative:
* OFF unless AR_CONFIG_EDIT=1 and AR_ENV_FILE points at the .env.
* Only the keys in SCHEMA can be written -- no arbitrary env injection.
* Secret values are never sent to the browser; a blank secret field means
  "keep the current value".
* Writes are line-preserving (comments and unlisted keys are untouched) and
  atomic. Changes take effect on the next app restart.
"""
import os
import re
import tempfile

from .. import config

# (key, label, kind, section). kind: text | int | bool | secret
SCHEMA = [
    ("AR_PLEX_BASE_URL", "Base URL", "text", "Plex"),
    ("AR_PLEX_TOKEN", "Token", "secret", "Plex"),
    ("AR_PLEX_VERIFY_SSL", "Verify TLS", "bool", "Plex"),
    ("AR_PLEX_MUSIC_SECTION", "Music library name", "text", "Plex"),
    ("AR_PLEX_CONCURRENCY", "Lookup concurrency", "int", "Plex"),

    ("AR_SPOTIFY_CLIENT_ID", "Client ID", "text", "Spotify"),
    ("AR_SPOTIFY_CLIENT_SECRET", "Client secret", "secret", "Spotify"),
    ("AR_SPOTIFY_REDIRECT_URI", "Redirect URI", "text", "Spotify"),
    ("AR_SPOTIFY_PLAYLIST_PUBLIC", "Public playlists", "bool", "Spotify"),

    ("AR_TIDAL_ENABLED", "Enabled", "bool", "Tidal"),

    ("AR_LASTFM_API_KEY", "API key", "secret", "Last.fm"),
    ("AR_LASTFM_SECRET", "Shared secret", "secret", "Last.fm"),
    ("AR_LASTFM_SESSION_KEY", "Session key", "secret", "Last.fm"),
    ("AR_SCROBBLE", "Scrobble", "bool", "Last.fm"),

    ("AR_LOCAL_RECOGNITION", "Local fingerprint cache", "bool", "Recognition"),
    ("AR_FP_MATCH_THRESHOLD", "Match threshold (0-1)", "text", "Recognition"),

    ("AR_WEB_USER", "Login username", "text", "Web login"),
    ("AR_WEB_PASSWORD_HASH", "Login password hash", "secret", "Web login"),

    ("AR_WATCHDOG_SILENCE_MIN", "Silence alert (min, 0=off)", "int", "Watchdog"),
    ("AR_NOTIFY_URL", "Alert webhook / ntfy URL", "text", "Watchdog"),
]

_KEYS = {k for k, _l, _t, _s in SCHEMA}
_KINDS = {k: t for k, _l, t, _s in SCHEMA}


def available() -> bool:
    return bool(config.CONFIG_EDIT_ENABLED and config.ENV_FILE)


def _parse(path: str) -> dict:
    out = {}
    try:
        with open(path) as f:
            for line in f:
                m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$", line)
                if m:
                    out[m.group(1)] = m.group(2)
    except FileNotFoundError:
        pass
    return out


def _effective(key: str, raw: dict):
    """Value currently in effect: the .env file wins; otherwise the resolved
    config default (so an unset on-by-default toggle doesn't render as off and
    get flipped to 0 on save)."""
    if key in raw:
        return raw[key]
    return getattr(config, key[3:], None)  # AR_PLEX_TOKEN -> config.PLEX_TOKEN


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip() in ("1", "true", "True", "yes", "on")


def fields_for_view():
    """Schema grouped by section, with current values suitable for the browser:
    non-secrets carry their effective value; secrets carry only is_set."""
    raw = _parse(config.ENV_FILE) if available() else {}
    sections: dict[str, list] = {}
    for key, label, kind, section in SCHEMA:
        eff = _effective(key, raw)
        item = {"key": key, "label": label, "kind": kind}
        if kind == "secret":
            item["is_set"] = bool(eff)
        elif kind == "bool":
            item["checked"] = _truthy(eff)
        else:
            item["value"] = "" if eff is None else str(eff)
        sections.setdefault(section, []).append(item)
    return sections


def _sanitize(kind: str, value: str) -> str:
    value = (value or "").replace("\n", " ").replace("\r", " ").strip()
    if kind == "bool":
        return "1" if value in ("1", "true", "on", "yes") else "0"
    if kind == "int":
        m = re.search(r"-?\d+", value)
        return m.group(0) if m else "0"
    return value


def apply_form(form) -> tuple[bool, str]:
    """Write submitted values back to the .env. `form` is the request form
    (a multidict). Returns (ok, message)."""
    if not available():
        return False, "Config editing is disabled."

    updates: dict[str, str] = {}
    for key in _KEYS:
        kind = _KINDS[key]
        if kind == "bool":
            # unchecked checkboxes are absent from the form
            updates[key] = "1" if form.get(key) is not None else "0"
        elif kind == "secret":
            v = form.get(key, "")
            if v.strip() == "":
                continue  # blank -> keep existing
            updates[key] = _sanitize("secret", v)
        else:
            if key in form:
                updates[key] = _sanitize(kind, form.get(key, ""))

    try:
        _write(config.ENV_FILE, updates)
    except OSError as e:
        return False, f"Could not write {config.ENV_FILE}: {e}"
    return True, "Saved. Restart the app for changes to take effect."


def _write(path: str, updates: dict) -> None:
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []

    out, seen = [], set()
    for line in lines:
        m = re.match(r"\s*#?\s*([A-Z0-9_]+)\s*=", line)
        key = m.group(1) if m else None
        if key in updates:
            if key in seen:
                continue  # drop duplicate definitions of an updated key
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")

    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
