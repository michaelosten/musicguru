"""Create a Tidal playlist from recognized tracks, via the unofficial tidalapi.

Tidal has no developer-app registration; it uses a device login. Authorize once
from the Pi:

    python -m audio_recognition.services.tidal login

It prints a link.tidal.com URL -- open it, approve, and the session is cached to
AR_TIDAL_TOKEN_CACHE and refreshed automatically afterward. Then the console's
"Create Tidal playlist" button works.

    python -m audio_recognition.services.tidal "Pink Floyd" "Time"   # search test
"""
import datetime
import json
import logging
import os
import time
import re
import unicodedata

from .. import config

log = logging.getLogger("audio_recognition.tidal")

_session = None
_pending = None   # in-progress web device login: {"session","future","link"}


def configured() -> bool:
    # Tidal needs no developer app or API key -- it's usable whenever the
    # library is installed. "Configured" just means available; the real gate is
    # whether you've connected (authorized) it.
    try:
        import tidalapi  # noqa: F401
        return True
    except Exception:
        return False


from ..textmatch import (norm as _norm, query_title as _clean,
                         query_name as _cleanname, titles_match, names_match)


def _save(session) -> None:
    d = os.path.dirname(config.TIDAL_TOKEN_CACHE)
    if d:
        os.makedirs(d, exist_ok=True)
    exp = session.expiry_time
    data = {
        "token_type": session.token_type,
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "expiry_time": exp.isoformat() if isinstance(exp, datetime.datetime) else None,
        "is_pkce": bool(getattr(session, "is_pkce", False)),
    }
    with open(config.TIDAL_TOKEN_CACHE, "w") as f:
        json.dump(data, f)


def _load_session():
    """Return a logged-in tidalapi Session from the cached token, refreshing the
    access token with the stored refresh token when it has expired. Only returns
    None if there's no usable token at all."""
    global _session
    if _session is not None:
        return _session
    if not configured() or not os.path.exists(config.TIDAL_TOKEN_CACHE):
        return None
    try:
        import tidalapi
        with open(config.TIDAL_TOKEN_CACHE) as f:
            data = json.load(f)
        exp = data.get("expiry_time")
        exp = datetime.datetime.fromisoformat(exp) if exp else None
        s = tidalapi.Session()
        s.load_oauth_session(
            data["token_type"], data["access_token"], data.get("refresh_token"),
            exp, data.get("is_pkce", False),
        )
        if not s.check_login():
            # Access token expired -> refresh with the long-lived refresh token
            # so the user never has to re-authorize by hand.
            rt = data.get("refresh_token")
            if not (rt and s.token_refresh(rt) and s.check_login()):
                log.warning("Tidal token refresh failed; re-authorization needed.")
                return None
        _session = s
        _save(s)   # persist the (possibly refreshed) access token + expiry
        return s
    except Exception as e:
        log.warning("Tidal session load failed: %s", e)
    return None


def _normalize_url(u: str) -> str:
    u = (u or "").strip()
    return u if u.startswith("http") else ("https://" + u if u else "")


def begin_login() -> dict | None:
    """Start a device-code login for the web UI. Returns the link + code to show
    the user; a background thread polls Tidal until they approve."""
    if not configured():
        return None
    global _pending
    _pending = None      # never reuse a previous (possibly expired) code
    try:
        import tidalapi
        s = tidalapi.Session()
        link, future = s.login_oauth()
        # Device codes are short-lived (~5 min). Track the deadline so a stale
        # code is never shown -- that's what produced "the device code you
        # entered has expired".
        ttl = int(getattr(link, "expires_in", 300) or 300)
        _pending = {"session": s, "future": future, "link": link,
                    "expires_at": time.time() + ttl}
        return {"url": _normalize_url(link.verification_uri_complete),
                "code": link.user_code, "expires_in": ttl}
    except Exception as e:
        log.warning("Tidal begin_login failed: %s", e)
        return None


def login_status() -> dict:
    """Poll target for the web login: reports pending / connected, saving the
    token once the user approves in their browser."""
    global _pending, _session
    if not _pending:
        return {"pending": False, "connected": connected()}
    s, future, link = _pending["session"], _pending["future"], _pending["link"]
    if future.done():
        try:
            future.result()
        except Exception as e:
            log.debug("Tidal login future: %s", e)
        if s.check_login():
            _session = s
            _save(s)
            _pending = None
            return {"pending": False, "connected": True}
        _pending = None
        return {"pending": False, "connected": False,
                "error": "Login wasn't approved before the code expired."}
    if time.time() >= _pending.get("expires_at", 0):
        # Code timed out without approval -> get a new one immediately, so the
        # screen always shows a code that still works.
        log.info("Tidal device code expired; issuing a fresh one")
        _pending = None
        fresh = begin_login()
        if fresh:
            return {"pending": True, "connected": False, "refreshed": True,
                    "url": fresh["url"], "code": fresh["code"],
                    "expires_in": fresh["expires_in"]}
        return {"pending": False, "connected": False,
                "error": "The code expired and a new one couldn't be requested."}
    return {"pending": True, "connected": False,
            "url": _normalize_url(link.verification_uri_complete),
            "code": link.user_code,
            "expires_in": max(0, int(_pending["expires_at"] - time.time()))}


def connected() -> bool:
    return _load_session() is not None


def authorize() -> bool:
    """One-time interactive device login (CLI). Prints a URL to approve."""
    try:
        import tidalapi
        s = tidalapi.Session()
        s.login_oauth_simple()   # blocks, prints the link.tidal.com URL
        if s.check_login():
            _save(s)
            return True
    except Exception as e:
        log.warning("Tidal authorize failed: %s", e)
    return False


def _tracks_from(results):
    tracks = getattr(results, "tracks", None)
    if tracks is None and isinstance(results, dict):
        tracks = results.get("tracks")
    return tracks or []


def search_id(session, artist: str, title: str, album: str = None) -> str | None:
    """Best Tidal track id for (artist, title), preferring a given album."""
    want_a, want_al = _norm(artist), (_norm(album) if album else "")
    matches = []
    for q in (f"{_cleanname(artist)} {_clean(title)}".strip(), _clean(title)):
        try:
            results = session.search(q, limit=10)
        except Exception as e:
            log.debug("Tidal search failed (%s): %s", q, e)
            continue
        for tr in _tracks_from(results):
            arts = [_norm(getattr(a, "name", "")) for a in (getattr(tr, "artists", []) or [])]
            main = getattr(tr, "artist", None)
            if main is not None:
                arts.append(_norm(getattr(main, "name", "")))
            title_ok = titles_match(title, getattr(tr, "name", ""))
            artist_ok = (not want_a) or any(
                want_a == a or want_a in a or a in want_a for a in arts if a) or any(
                names_match(artist, getattr(x, "name", "")) for x in
                ((getattr(tr, "artists", []) or []) + ([getattr(tr, "artist", None)]
                 if getattr(tr, "artist", None) is not None else [])))
            if title_ok and artist_ok and getattr(tr, "id", None) is not None:
                matches.append(tr)
        if matches:
            break
    if not matches:
        return None
    if want_al:
        for tr in matches:
            alb = getattr(tr, "album", None)
            it_al = _norm(getattr(alb, "name", "")) if alb is not None else ""
            if it_al and (it_al == want_al or want_al in it_al or it_al in want_al):
                return str(getattr(tr, "id"))
    return str(getattr(matches[0], "id"))


def create_playlist(name: str, tracks: list) -> dict:
    """tracks: [{'artist','title'}]. Returns {'created','url','added','skipped'}."""
    s = _load_session()
    if s is None:
        raise RuntimeError("Tidal is not authorized -- run: "
                           "python -m audio_recognition.services.tidal login")
    ids, skipped, seen = [], 0, set()
    for t in tracks:
        tid = search_id(s, t.get("artist", ""), t.get("title", ""), t.get("album"))
        if tid and tid not in seen:
            ids.append(tid)
            seen.add(tid)
        elif not tid:
            skipped += 1
    if not ids:
        raise RuntimeError("none of these tracks were found on Tidal")

    pl = s.user.create_playlist(name, "Created by musicguru")
    for i in range(0, len(ids), 100):
        pl.add(ids[i:i + 100])
    return {"created": True, "url": f"https://tidal.com/browse/playlist/{pl.id}",
            "added": len(ids), "skipped": skipped}


_pl_cache: dict[str, str] = {}   # playlist name -> playlist id (auto-playlist)
_pl_tracks: dict[str, set] = {}  # playlist id -> set of track ids already in it


def _playlist_id(s, name: str) -> str:
    if name in _pl_cache:
        return _pl_cache[name]
    try:
        for pl in s.user.playlists():
            if (getattr(pl, "name", "") or "") == name:
                _pl_cache[name] = pl.id
                return pl.id
    except Exception as e:
        log.debug("Tidal playlist list failed: %s", e)
    pl = s.user.create_playlist(name, "Auto-generated by musicguru")
    _pl_cache[name] = pl.id
    return pl.id


def _fresh_playlist(s, pid: str):
    """A freshly fetched editable playlist, so its ETag is current. Reusing a
    cached playlist object makes every add after the first fail (412)."""
    from tidalapi.playlist import UserPlaylist
    return UserPlaylist(s, pid)


def _existing_ids(s, pid: str) -> set:
    """Track ids already in the playlist, fetched once and cached, so we skip a
    track that's already there (added manually or on a previous run)."""
    if pid in _pl_tracks:
        return _pl_tracks[pid]
    ids = set()
    try:
        pl = _fresh_playlist(s, pid)
        offset = 0
        while True:
            batch = pl.tracks(limit=100, offset=offset)
            if not batch:
                break
            for tr in batch:
                if getattr(tr, "id", None) is not None:
                    ids.add(str(tr.id))
            if len(batch) < 100:
                break
            offset += 100
    except Exception as e:
        log.debug("Tidal playlist tracks fetch failed: %s", e)
    _pl_tracks[pid] = ids
    return ids


def add_to_named_playlist(name: str, artist: str, title: str, album: str = None) -> str:
    """Append one track to the named playlist (create it if needed). Returns
    'added', 'present' (already in the playlist), or 'absent' (not found)."""
    s = _load_session()
    if s is None:
        raise RuntimeError("Tidal not connected")
    tid = search_id(s, artist, title, album)
    if not tid:
        return "absent"
    pid = _playlist_id(s, name)
    existing = _existing_ids(s, pid)
    if str(tid) in existing:
        return "present"
    try:
        _fresh_playlist(s, pid).add([tid])
    except Exception:
        # Stale ETag or transient error -> re-fetch fresh and retry once.
        _fresh_playlist(s, pid).add([tid])
    existing.add(str(tid))
    return "added"


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if not configured():
        print("Tidal is unavailable -- install it with: pip install tidalapi")
        sys.exit(1)
    if len(sys.argv) >= 2 and sys.argv[1] == "login":
        print("Opening Tidal device login -- approve the printed URL in a browser...")
        print("authorized" if authorize() else "authorization failed")
        sys.exit(0)
    print(f"configured: {configured()}  connected: {connected()}")
    if not connected():
        print("Not authorized yet. Run:  python -m audio_recognition.services.tidal login")
        sys.exit(1)
    if len(sys.argv) >= 3:
        s = _load_session()
        print(f"search {sys.argv[1]!r} - {sys.argv[2]!r} -> id {search_id(s, sys.argv[1], sys.argv[2])}")


def reset() -> None:
    """Drop the cached session and playlist caches so a token/path change (after a
    config reload) is picked up on the next call."""
    global _session
    _session = None
    _pl_cache.clear()
    _pl_tracks.clear()
