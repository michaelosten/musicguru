"""Resolve recognized (artist, title) pairs to real tracks in a Plex library,
using the maintained python-plexapi client.

Designed to scale to very large libraries (100k+ tracks): it never bulk-fetches
the library. Instead it looks up only the tracks you've actually recognized --
each a fast, server-side indexed title search -- and runs the want-list's
hundreds of lookups concurrently, caching results per process.

Public surface (unchanged for callers):
    configured()                       -> bool
    in_library(artist, title)          -> bool
    presence_batch(pairs)              -> {(artist,title): bool}
    find_track(artist, title)          -> dict | None       # streamable match
    open_stream(part_key, range)       -> requests.Response
    create_or_append_playlist(name, rating_keys) -> dict

Diagnostic:
    python -m audio_recognition.plex.client "Pink Floyd" "Signs of Life"
"""
import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import requests

from ..config import (
    PLEX_BASE_URL, PLEX_CONCURRENCY, PLEX_MUSIC_SECTION, PLEX_TIMEOUT, PLEX_TOKEN,
    PLEX_VERIFY_SSL,
)

log = logging.getLogger("audio_recognition.plex")

_cache: dict[tuple[str, str], dict | None] = {}
_server = None
_section = None
_connect_tried = False


def configured() -> bool:
    return bool(PLEX_BASE_URL and PLEX_TOKEN)


def _base_url() -> str:
    """Tolerate a scheme-less base URL like '192.168.1.205:32400' by assuming
    http:// (requests needs a scheme or it errors with 'No connection adapters')."""
    u = (PLEX_BASE_URL or "").strip()
    if u and not re.match(r"^https?://", u, re.I):
        u = "http://" + u
    return u


from ..textmatch import norm as _norm, query_title as _query_title, titles_match


def _session() -> requests.Session:
    s = requests.Session()
    if not PLEX_VERIFY_SSL:
        s.verify = False
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
    return s


def connect():
    """Connect and resolve the music section once. Returns (server, section) or
    (None, None). Cached; safe to call repeatedly."""
    global _server, _section, _connect_tried
    if _connect_tried:
        return _server, _section
    _connect_tried = True
    if not configured():
        return None, None
    try:
        from plexapi.server import PlexServer
        _server = PlexServer(_base_url(), PLEX_TOKEN, session=_session(),
                             timeout=int(PLEX_TIMEOUT))
    except Exception as e:
        log.warning("Plex connect failed (%s): %s", _base_url(), e)
        _server = None
        return None, None
    try:
        sections = [s for s in _server.library.sections() if s.TYPE == "artist"]
        if PLEX_MUSIC_SECTION:
            _section = next((s for s in sections if s.title == PLEX_MUSIC_SECTION), None)
            if _section is None:
                log.warning("Plex music section %r not found; have: %s",
                            PLEX_MUSIC_SECTION, [s.title for s in sections])
        if _section is None:
            _section = sections[0] if sections else None
    except Exception as e:
        log.warning("Plex section lookup failed: %s", e)
        _section = None
    return _server, _section


def _search(section, title: str):
    """Best-effort candidate tracks for a title, tried a few ways for recall."""
    q = _query_title(title) or title
    for attempt in (
        lambda: section.searchTracks(title=q, maxresults=60),
        lambda: section.searchTracks(**{"track.title": q}, maxresults=60),
    ):
        try:
            hits = attempt()
            if hits:
                return hits
        except Exception as e:
            log.debug("Plex track search variant failed: %s", e)
    try:
        return [h for h in _server.search(q, mediatype="track")]
    except Exception as e:
        log.debug("Plex hub search failed: %s", e)
        return []


def _match(artist: str, title: str) -> dict | None:
    if not configured():
        return None
    ck = (_norm(artist), _norm(title))
    if ck in _cache:
        return _cache[ck]

    _srv, section = connect()
    if section is None:
        return None  # connection/section problem: don't cache, might be transient

    want_artist, want_title = ck
    if not want_title:
        _cache[ck] = None
        return None

    best = best_any = None
    try:
        candidates = _search(section, title)
    except Exception as e:
        log.warning("Plex search failed for %s - %s: %s", artist, title, e)
        return None

    for tr in candidates:
        item_title = _norm(getattr(tr, "title", ""))
        item_artist = _norm(getattr(tr, "grandparentTitle", ""))
        if not item_title:
            continue
        title_ok = titles_match(title, getattr(tr, "title", ""))
        artist_ok = (not want_artist
                     or want_artist in item_artist or item_artist in want_artist)
        if not (title_ok and artist_ok):
            continue
        exact = item_title == want_title and item_artist == want_artist
        part_key = None
        try:
            part_key = tr.media[0].parts[0].key
        except (AttributeError, IndexError):
            pass
        dur = getattr(tr, "duration", None)
        cand = {
            "rating_key": str(getattr(tr, "ratingKey", "")) or None,
            "part_key": part_key,
            "duration": int(dur / 1000) if dur else None,
            "title": getattr(tr, "title", None),
            "artist": getattr(tr, "grandparentTitle", None),
        }
        if best_any is None or exact:
            best_any = cand
        if part_key and (best is None or exact):
            best = cand
        if exact and part_key:
            break

    result = best or best_any
    if result is None:
        log.info("No Plex match for %s - %s", artist, title)
    _cache[ck] = result
    return result


def find_track(artist: str, title: str) -> dict | None:
    """A streamable match (guaranteed part_key) -- for streaming and M3U export."""
    m = _match(artist, title)
    return m if m and m.get("part_key") else None


def in_library(artist: str, title: str) -> bool:
    """Whether the library has this track at all, streamable or not."""
    return _match(artist, title) is not None


def presence_batch(pairs: list) -> dict:
    """Check many (artist, title) pairs at once, concurrently. Returns
    {(artist, title): bool}. Used by the want-list and the in-library badge so a
    few hundred lookups take seconds, not minutes -- results are cached, so a
    second call is instant."""
    pairs = list(pairs)
    if not configured() or not pairs:
        return {p: False for p in pairs}
    connect()  # establish the shared connection once before fanning out
    workers = max(1, min(PLEX_CONCURRENCY, len(pairs)))

    def _one(p):
        return p, (_match(p[0], p[1]) is not None)

    out: dict = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for p, present in ex.map(_one, pairs):
            out[p] = present
    return out


def open_stream(part_key: str, range_header: str | None = None) -> requests.Response:
    """Streaming GET of a Plex part, honoring the client's Range header. The
    token stays server-side."""
    headers = {"X-Plex-Token": PLEX_TOKEN}
    if range_header:
        headers["Range"] = range_header
    sep = "&" if "?" in part_key else "?"
    return _session().get(
        f"{_base_url()}{part_key}{sep}X-Plex-Token={PLEX_TOKEN}",
        headers=headers, stream=True, timeout=PLEX_TIMEOUT,
    )


def create_or_append_playlist(title: str, rating_keys: list) -> dict:
    """Create an audio playlist from these rating keys, or append to an existing
    one with the same title. Returns {'created': bool, 'playlist_key': str|None}."""
    server, _section = connect()
    if server is None:
        raise RuntimeError("not connected to Plex")

    items = []
    for rk in rating_keys:
        try:
            items.append(server.fetchItem(int(rk)))
        except Exception as e:
            log.debug("Plex fetchItem %s failed: %s", rk, e)
    if not items:
        raise RuntimeError("no resolvable tracks for playlist")

    existing = None
    try:
        for pl in server.playlists(playlistType="audio"):
            if (pl.title or "").strip().lower() == title.strip().lower():
                existing = pl
                break
    except Exception as e:
        log.debug("Plex playlist list failed: %s", e)

    if existing is not None:
        existing.addItems(items)
        return {"created": False, "playlist_key": str(existing.ratingKey)}

    from plexapi.playlist import Playlist
    pl = Playlist.create(server, title, items=items)
    return {"created": True, "playlist_key": str(getattr(pl, "ratingKey", "")) or None}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG)
    print(f"configured: {configured()}  base_url: {_base_url()!r}  "
          f"verify_ssl: {PLEX_VERIFY_SSL}")
    srv, sec = connect()
    if srv is None:
        print("=> could NOT connect to Plex. Check AR_PLEX_BASE_URL / AR_PLEX_TOKEN "
              "(and set AR_PLEX_VERIFY_SSL=0 if you use https://<ip>).")
        sys.exit(1)
    try:
        allsecs = [(s.title, s.TYPE) for s in srv.library.sections()]
    except Exception as e:
        allsecs = f"<error: {e}>"
    print(f"connected. sections: {allsecs}")
    print(f"music section in use: {sec.title if sec else None}")
    if len(sys.argv) >= 3:
        artist, title = sys.argv[1], sys.argv[2]
        print(f"\nsearching for: {artist!r} - {title!r}")
        cands = _search(sec, title) if sec else []
        print(f"raw candidates ({len(cands)}):")
        for tr in cands[:15]:
            print(f"  - {getattr(tr,'grandparentTitle',None)!r} / "
                  f"{getattr(tr,'title',None)!r} (ratingKey={getattr(tr,'ratingKey',None)})")
        print(f"\nin_library: {in_library(artist, title)}")
        print(f"find_track: {find_track(artist, title)}")
    else:
        print('\nPass an artist and title to test matching, e.g.:\n'
              '  python -m audio_recognition.plex.client "Pink Floyd" "Signs of Life"')
