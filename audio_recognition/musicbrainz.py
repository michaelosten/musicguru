"""Look up candidate releases (albums) for a track from MusicBrainz, so a
mis-attributed album ("Greatest Hits", a compilation) can be corrected to the
original release it appeared on.

MusicBrainz asks for a descriptive User-Agent and ~1 request/second; both are
honored. Results put original studio albums first and push
compilations/live/soundtracks down.
"""
import logging
import re
import threading
import time
import urllib.parse

import requests

from . import __version__

log = logging.getLogger("audio_recognition.musicbrainz")

_UA = f"musicguru/{__version__} (https://github.com/michaelosten/musicguru)"
_BASE = "https://musicbrainz.org/ws/2"
_CAA = "https://coverartarchive.org/release-group"

_lock = threading.Lock()
_last = 0.0
_cache: dict[str, list] = {}


def _throttle():
    global _last
    with _lock:
        wait = 1.05 - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.time()


def _kind(primary: str, secondary: list) -> str:
    if secondary:
        return secondary[0]           # Compilation / Live / Soundtrack / Remix
    return primary or "Other"


def _rank(kind: str, year: str) -> tuple:
    # Originals (plain Album/EP/Single) first, then everything else; oldest first.
    demote = 0 if kind in ("Album", "EP", "Single") else 1
    return (demote, year or "9999")


def search_releases(artist: str, title: str, limit: int = 8) -> list[dict]:
    """Candidate releases for (artist, title). Each: album, year, type,
    is_original, mbid (release-group), cover_url."""
    key = f"{artist}\x1f{title}".lower()
    if key in _cache:
        return _cache[key]
    q = f'recording:"{title}" AND artist:"{artist}"'
    url = f"{_BASE}/recording?query={urllib.parse.quote(q)}&fmt=json&limit=25"
    try:
        _throttle()
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=8)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("MusicBrainz lookup failed for %s - %s: %s", artist, title, e)
        return []

    groups: dict[str, dict] = {}
    for rec in data.get("recordings", []):
        for rel in rec.get("releases", []):
            rg = rel.get("release-group") or {}
            gid = rg.get("id")
            if not gid:
                continue
            year = (rel.get("date") or rg.get("first-release-date") or "")[:4]
            kind = _kind(rg.get("primary-type", ""), rg.get("secondary-types", []))
            prev = groups.get(gid)
            if prev is None or (year and year < prev["year"]):
                groups[gid] = {
                    "album": rg.get("title", ""),
                    "year": year,
                    "type": kind,
                    "is_original": kind in ("Album", "EP", "Single"),
                    "mbid": gid,
                    "cover_url": f"{_CAA}/{gid}/front-250",
                }

    out = sorted(groups.values(), key=lambda g: _rank(g["type"], g["year"]))
    out = out[:limit]
    _cache[key] = out
    return out


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 3:
        print('usage: python -m audio_recognition.musicbrainz "Artist" "Title"')
        sys.exit(1)
    for r in search_releases(sys.argv[1], sys.argv[2]):
        star = "*" if r["is_original"] else " "
        print(f'{star} {r["year"] or "----"}  {r["type"]:<12}  {r["album"]}')
