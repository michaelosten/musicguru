"""Album/release candidates for a track from Last.fm, for the Release picker.

Uses read-only endpoints (just the API key you already set for scrobbling):
track.getInfo for the album the track is on, and artist.getTopAlbums for the
rest of that artist's albums as options.
"""
import logging

import requests

from . import config

log = logging.getLogger("audio_recognition.lastfm")

_URL = "https://ws.audioscrobbler.com/2.0/"


def configured() -> bool:
    return bool(config.LASTFM_API_KEY)


def _largest_image(images) -> str:
    order = {"small": 0, "medium": 1, "large": 2, "extralarge": 3, "mega": 4}
    best, best_sz = "", -1
    for im in images or []:
        url, sz = im.get("#text"), order.get(im.get("size"), -1)
        if url and sz > best_sz:
            best, best_sz = url, sz
    return best


def _get(method, **params):
    params.update({"method": method, "api_key": config.LASTFM_API_KEY,
                   "autocorrect": 1, "format": "json"})
    r = requests.get(_URL, params=params, timeout=8)
    r.raise_for_status()
    return r.json()


def search_releases(artist: str, title: str, limit: int = 12) -> list[dict]:
    """Candidate albums for (artist, title). The album the track is on is listed
    first (flagged), followed by the artist's other albums."""
    if not configured():
        return []
    out, seen = [], set()

    try:
        j = _get("track.getInfo", artist=artist, track=title)
        alb = (j.get("track") or {}).get("album") or {}
        name = alb.get("title")
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append({"album": name, "year": "", "type": "on this track",
                        "is_original": True, "cover_url": _largest_image(alb.get("image")),
                        "mbid": alb.get("mbid", "")})
    except Exception as e:
        log.debug("Last.fm track.getInfo failed: %s", e)

    try:
        j = _get("artist.getTopAlbums", artist=artist, limit=25)
        for a in (j.get("topalbums") or {}).get("album", []):
            name = a.get("name")
            if not name or name.lower() in seen or name.strip().lower() in ("(null)", "null"):
                continue
            seen.add(name.lower())
            out.append({"album": name, "year": "", "type": "album",
                        "is_original": False, "cover_url": _largest_image(a.get("image")),
                        "mbid": a.get("mbid", "")})
            if len(out) >= limit:
                break
    except Exception as e:
        log.debug("Last.fm artist.getTopAlbums failed: %s", e)

    return out[:limit]


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 3:
        print('usage: python -m audio_recognition.lastfm "Artist" "Title"')
        sys.exit(1)
    print(f"configured: {configured()}")
    for r in search_releases(sys.argv[1], sys.argv[2]):
        print(("* " if r["is_original"] else "  ") + f'{r["type"]:<12} {r["album"]}')
