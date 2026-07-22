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
import difflib
import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import requests

from .. import config

log = logging.getLogger("audio_recognition.plex")


def _is_conn_error(e: Exception) -> bool:
    """Network/server-side failure (as opposed to 'no results'), so the caller
    retries later instead of recording a permanent miss."""
    if isinstance(e, (requests.exceptions.ConnectionError,
                      requests.exceptions.Timeout,
                      requests.exceptions.SSLError)):
        return True
    txt = f"{type(e).__name__}: {e}".lower()
    return any(k in txt for k in (
        "connection", "timeout", "timed out", "unreachable", "refused",
        "reset by peer", "temporarily unavailable", "bad gateway",
        "service unavailable", "502", "503", "504", "broken pipe",
    ))

_cache: dict[tuple[str, str], dict | None] = {}
_server = None
_section = None
_connect_tried = False


def reset() -> None:
    """Drop the cached connection and match cache so a credential change (after a
    config reload) reconnects with the new base URL/token."""
    global _server, _section, _connect_tried
    _server = None
    _section = None
    _connect_tried = False
    _cache.clear()


def configured() -> bool:
    return bool(config.PLEX_BASE_URL and config.PLEX_TOKEN)


def _base_url() -> str:
    """Tolerate a scheme-less base URL like '192.168.1.205:32400' by assuming
    http:// (requests needs a scheme or it errors with 'No connection adapters')."""
    u = (config.PLEX_BASE_URL or "").strip()
    if u and not re.match(r"^https?://", u, re.I):
        u = "http://" + u
    return u


from ..textmatch import (norm as _norm, query_title as _query_title,
                         query_name as _query_name, titles_match, names_match)


def _session() -> requests.Session:
    s = requests.Session()
    if not config.PLEX_VERIFY_SSL:
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
        _server = PlexServer(_base_url(), config.PLEX_TOKEN, session=_session(),
                             timeout=int(config.PLEX_TIMEOUT))
    except Exception as e:
        log.warning("Plex connect failed (%s): %s", _base_url(), e)
        _server = None
        return None, None
    try:
        sections = [s for s in _server.library.sections() if s.TYPE == "artist"]
        if config.PLEX_MUSIC_SECTION:
            _section = next((s for s in sections if s.title == config.PLEX_MUSIC_SECTION), None)
            if _section is None:
                log.warning("Plex music section %r not found; have: %s",
                            config.PLEX_MUSIC_SECTION, [s.title for s in sections])
        if _section is None:
            _section = sections[0] if sections else None
    except Exception as e:
        log.warning("Plex section lookup failed: %s", e)
        _section = None
    return _server, _section


class PlexUnavailable(Exception):
    """Plex couldn't be reached. Distinct from 'track isn't in the library' so
    callers can retry later instead of recording a permanent miss."""


def _title_variants(title: str) -> list:
    """Progressively looser query forms, for recall against messy recognizer
    titles ('Wings for Marie, Pt. 1', 'Schissm')."""
    out, seen = [], set()

    def add(v):
        v = (v or "").strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)

    base = _query_title(title) or title or ""
    add(base)
    add(title)
    # Drop a trailing part/disc marker: "Wings for Marie, Pt. 1" -> "Wings for Marie"
    add(re.sub(r"[,\-–(]?\s*\b(pt|part)\b\.?\s*[ivx\d]+\s*\)?$", "", base, flags=re.I))
    # Normalize punctuation to spaces, and strip featured-artist tails.
    add(re.sub(r"\s*\((feat|ft|featuring)\.?[^)]*\)", "", base, flags=re.I))
    add(re.sub(r"[^\w\s]+", " ", base))
    # First few words -- catches long titles and trailing junk.
    words = re.sub(r"[^\w\s]+", " ", base).split()
    if len(words) > 2:
        add(" ".join(words[:3]))
    if len(words) > 1:
        add(" ".join(words[:2]))
    return out


def _fuzzy(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _search(section, title: str, artist: str = None):
    """Candidate tracks, tried several ways for recall. Raises PlexUnavailable if
    Plex itself is unreachable (so it's retried, not recorded as a miss)."""
    hits, errors = [], 0
    for q in _title_variants(title):
        for attempt in (
            lambda q=q: section.searchTracks(title=q, maxresults=60),
            lambda q=q: section.searchTracks(**{"track.title": q}, maxresults=60),
        ):
            try:
                found = attempt()
                if found:
                    return found
            except Exception as e:
                errors += 1
                if _is_conn_error(e):
                    raise PlexUnavailable(str(e))
                log.debug("Plex track search variant failed: %s", e)

    # Last resort: pull the artist's tracks and fuzzy-match locally. This is what
    # catches recognizer typos like "Schissm" -> "Schism".
    if artist:
        try:
            for art in section.searchArtists(title=(_query_name(artist) or artist),
                                             maxresults=3):
                try:
                    hits.extend(art.tracks())
                except Exception as e:
                    if _is_conn_error(e):
                        raise PlexUnavailable(str(e))
            if hits:
                return hits
        except PlexUnavailable:
            raise
        except Exception as e:
            if _is_conn_error(e):
                raise PlexUnavailable(str(e))
            log.debug("Plex artist search failed: %s", e)

    try:
        return [h for h in _server.search(_query_title(title) or title, mediatype="track")]
    except Exception as e:
        if _is_conn_error(e):
            raise PlexUnavailable(str(e))
        log.debug("Plex hub search failed: %s", e)
        return []


def _match(artist: str, title: str, album: str = None) -> dict | None:
    if not configured():
        return None
    want_album = _norm(album) if album else ""
    ck = (_norm(artist), _norm(title), want_album)
    if ck in _cache:
        return _cache[ck]

    _srv, section = connect()
    if section is None:
        # Can't tell "no match" from "can't reach Plex" -- treat as unavailable so
        # the caller retries rather than recording a permanent miss.
        raise PlexUnavailable(f"not connected ({_base_url() or 'no base url'})")

    want_artist, want_title, _ = ck
    if not want_title:
        _cache[ck] = None
        return None

    # A manual assignment wins over any search heuristics.
    try:
        from ..storage.db import get_library_link
        link = get_library_link(artist, title)
    except Exception:
        link = None
    if link and link.get("item_key"):
        try:
            tr = _srv.fetchItem(int(link["item_key"]))
            part = None
            try:
                part = tr.media[0].parts[0].key
            except (AttributeError, IndexError):
                pass
            res = {"rating_key": str(getattr(tr, "ratingKey", link["item_key"])),
                   "part_key": part,
                   "duration": int(getattr(tr, "duration", 0) / 1000) or None,
                   "title": getattr(tr, "title", None),
                   "artist": getattr(tr, "grandparentTitle", None)}
            _cache[ck] = res
            return res
        except Exception as e:
            if _is_conn_error(e):
                raise PlexUnavailable(str(e))
            log.warning("Plex link %s no longer resolves: %s", link["item_key"], e)

    best, best_score = None, -(10 ** 6)
    candidates = _search(section, title, artist)   # PlexUnavailable propagates

    for tr in candidates:
        item_title = _norm(getattr(tr, "title", ""))
        item_artist = _norm(getattr(tr, "grandparentTitle", ""))
        if not item_title:
            continue
        artist_ok = (not want_artist
                     or want_artist in item_artist or item_artist in want_artist
                     or _fuzzy(want_artist, item_artist) >= 0.85
                     or names_match(artist, getattr(tr, "grandparentTitle", "")))
        if not artist_ok:
            continue
        title_ok = titles_match(title, getattr(tr, "title", ""))
        near = False
        if not title_ok:
            # Recognizer typos ("Schissm" vs "Schism") and small punctuation
            # differences: accept a close title when the artist already matches.
            ratio = _fuzzy(want_title, item_title)
            near = ratio >= 0.86 or (
                len(want_title) >= 6 and ratio >= 0.80
                and abs(len(want_title) - len(item_title)) <= 3)
            if not near:
                continue
        item_album = _norm(getattr(tr, "parentTitle", ""))
        album_ok = bool(want_album) and (item_album == want_album
                                         or want_album in item_album or item_album in want_album)
        exact = item_title == want_title and item_artist == want_artist
        part_key = None
        try:
            part_key = tr.media[0].parts[0].key
        except (AttributeError, IndexError):
            pass
        # Prefer, in order: the pinned album, a streamable copy, an exact label.
        score = ((4 if album_ok else 0) + (2 if part_key else 0)
                 + (1 if exact else 0) - (3 if near else 0))
        if score > best_score:
            best_score = score
            best = {
                "rating_key": str(getattr(tr, "ratingKey", "")) or None,
                "part_key": part_key,
                "duration": int(getattr(tr, "duration", 0) / 1000) or None,
                "title": getattr(tr, "title", None),
                "artist": getattr(tr, "grandparentTitle", None),
            }
        if album_ok and part_key:
            break   # best possible: right album, streamable

    if best is None:
        log.info("No Plex match for %s - %s", artist, title)
    elif _norm(best.get("title") or "") != want_title:
        log.info("Plex fuzzy match: %s - %s -> %s - %s", artist, title,
                 best.get("artist"), best.get("title"))
    _cache[ck] = best
    return best


def find_track(artist: str, title: str, album: str = None) -> dict | None:
    """A streamable match (guaranteed part_key) -- for streaming and M3U export."""
    m = _match(artist, title, album)
    return m if m and m.get("part_key") else None


def in_library(artist: str, title: str) -> bool:
    """Whether the library has this track at all, streamable or not. Returns
    False (rather than raising) if Plex is unreachable -- this drives UI badges."""
    try:
        return _match(artist, title) is not None
    except PlexUnavailable:
        return False


def match_rating_key(artist: str, title: str, album: str = None) -> str | None:
    """Rating key of a matching library track, for adding to a playlist. Unlike
    find_track this does NOT require a streamable part_key -- a track can be in a
    playlist even if the search result didn't carry its media parts."""
    m = _match(artist, title, album)
    return m.get("rating_key") if m else None


def browse_artists(query: str, limit: int = 20) -> list:
    """Artists whose name matches -- step 1 of the manual picker."""
    _srv, section = connect()
    if section is None:
        raise PlexUnavailable("not connected")
    q = (query or "").strip()
    if not q:
        return []
    try:
        arts = section.searchArtists(title=q, maxresults=limit) or []
    except Exception as e:
        if _is_conn_error(e):
            raise PlexUnavailable(str(e))
        log.debug("Plex artist browse failed: %s", e)
        return []
    return [{"key": str(getattr(a, "ratingKey", "")),
             "name": getattr(a, "title", "") or ""} for a in arts
            if getattr(a, "ratingKey", None) is not None]


def browse_albums(artist_key: str, limit: int = 100) -> list:
    """Albums for an artist -- step 2."""
    srv, _section = connect()
    if srv is None:
        raise PlexUnavailable("not connected")
    try:
        art = srv.fetchItem(int(artist_key))
        albums = art.albums() or []
    except Exception as e:
        if _is_conn_error(e):
            raise PlexUnavailable(str(e))
        log.debug("Plex album browse failed: %s", e)
        return []
    out = []
    for al in albums[:limit]:
        if getattr(al, "ratingKey", None) is None:
            continue
        out.append({"key": str(al.ratingKey),
                    "title": getattr(al, "title", "") or "",
                    "year": str(getattr(al, "year", "") or "")})
    return out


def browse_tracks(album_key: str, limit: int = 200) -> list:
    """Tracks on an album -- step 3."""
    srv, _section = connect()
    if srv is None:
        raise PlexUnavailable("not connected")
    try:
        al = srv.fetchItem(int(album_key))
        tracks = al.tracks() or []
    except Exception as e:
        if _is_conn_error(e):
            raise PlexUnavailable(str(e))
        log.debug("Plex track browse failed: %s", e)
        return []
    out = []
    for tr in tracks[:limit]:
        if getattr(tr, "ratingKey", None) is None:
            continue
        out.append({"rating_key": str(tr.ratingKey),
                    "title": getattr(tr, "title", "") or "",
                    "artist": getattr(tr, "grandparentTitle", "") or "",
                    "album": getattr(tr, "parentTitle", "") or "",
                    "index": getattr(tr, "index", None)})
    return out


def search_candidates(query: str, limit: int = 25) -> list:
    """Free-text track search for the manual 'find it in Plex' picker."""
    _srv, section = connect()
    if section is None:
        raise PlexUnavailable("not connected")
    q = (query or "").strip()
    if not q:
        return []
    hits = []
    try:
        hits = section.searchTracks(title=q, maxresults=limit) or []
    except Exception as e:
        if _is_conn_error(e):
            raise PlexUnavailable(str(e))
        log.debug("Plex candidate search failed: %s", e)
    if not hits:
        try:
            for art in section.searchArtists(title=q, maxresults=2):
                hits.extend(art.tracks() or [])
        except Exception as e:
            if _is_conn_error(e):
                raise PlexUnavailable(str(e))
    out = []
    for tr in hits[:limit]:
        rk = getattr(tr, "ratingKey", None)
        if rk is None:
            continue
        out.append({"rating_key": str(rk),
                    "title": getattr(tr, "title", "") or "",
                    "artist": getattr(tr, "grandparentTitle", "") or "",
                    "album": getattr(tr, "parentTitle", "") or ""})
    return out


def clear_match_cache() -> int:
    """Forget cached match results so lookups re-run (e.g. after a matching
    improvement, or to re-check the want-list). Returns entries cleared."""
    n = len(_cache)
    _cache.clear()
    return n


def presence_batch(pairs: list) -> dict:
    """Check many (artist, title) pairs at once, concurrently. Returns
    {(artist, title): bool}. Used by the want-list and the in-library badge so a
    few hundred lookups take seconds, not minutes -- results are cached, so a
    second call is instant."""
    pairs = list(pairs)
    if not configured() or not pairs:
        return {p: None for p in pairs}
    connect()  # establish the shared connection once before fanning out
    workers = max(1, min(config.PLEX_CONCURRENCY, len(pairs)))

    def _one(p):
        try:
            return p, (_match(p[0], p[1]) is not None)
        except PlexUnavailable:
            # UNKNOWN, not "missing" -- claiming a track isn't in Plex because we
            # couldn't reach Plex is what filled the want-list with owned music.
            return p, None

    out: dict = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for p, present in ex.map(_one, pairs):
            out[p] = present
    return out


def open_stream(part_key: str, range_header: str | None = None) -> requests.Response:
    """Streaming GET of a Plex part, honoring the client's Range header. The
    token stays server-side."""
    headers = {"X-Plex-Token": config.PLEX_TOKEN}
    if range_header:
        headers["Range"] = range_header
    sep = "&" if "?" in part_key else "?"
    return _session().get(
        f"{_base_url()}{part_key}{sep}X-Plex-Token={config.PLEX_TOKEN}",
        headers=headers, stream=True, timeout=config.PLEX_TIMEOUT,
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
        # Skip items already in the playlist.
        have = set()
        try:
            have = {str(it.ratingKey) for it in existing.items()}
        except Exception as e:
            log.debug("Plex playlist items fetch failed: %s", e)
        new_items = [it for it in items if str(getattr(it, "ratingKey", "")) not in have]
        if new_items:
            try:
                existing.addItems(new_items)
            except Exception as e:
                # Some Plex versions reject a whole batch if one item is already
                # in the playlist; add individually so one dup/bad item doesn't
                # block the rest.
                log.debug("Plex addItems batch failed (%s); adding individually", e)
                for it in new_items:
                    try:
                        existing.addItems([it])
                    except Exception as e2:
                        log.debug("Plex addItem skipped: %s", e2)
        return {"created": False, "playlist_key": str(existing.ratingKey),
                "added": len(new_items), "skipped": len(items) - len(new_items)}

    from plexapi.playlist import Playlist
    pl = Playlist.create(server, title, items=items)
    return {"created": True, "playlist_key": str(getattr(pl, "ratingKey", "")) or None,
            "added": len(items), "skipped": 0}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG)
    print(f"configured: {configured()}  base_url: {_base_url()!r}  "
          f"verify_ssl: {config.PLEX_VERIFY_SSL}")
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
