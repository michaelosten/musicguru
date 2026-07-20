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
import re
import unicodedata

from ..config import TIDAL_ENABLED, TIDAL_TOKEN_CACHE

log = logging.getLogger("audio_recognition.tidal")

_session = None
_pending = None   # in-progress web device login: {"session","future","link"}


def configured() -> bool:
    return bool(TIDAL_ENABLED)


from ..textmatch import norm as _norm, query_title as _clean, titles_match


def _save(session) -> None:
    d = os.path.dirname(TIDAL_TOKEN_CACHE)
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
    with open(TIDAL_TOKEN_CACHE, "w") as f:
        json.dump(data, f)


def _load_session():
    """Return a logged-in tidalapi Session from the cached token, refreshing the
    access token with the stored refresh token when it has expired. Only returns
    None if there's no usable token at all."""
    global _session
    if _session is not None:
        return _session
    if not configured() or not os.path.exists(TIDAL_TOKEN_CACHE):
        return None
    try:
        import tidalapi
        with open(TIDAL_TOKEN_CACHE) as f:
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
    try:
        import tidalapi
        s = tidalapi.Session()
        link, future = s.login_oauth()
        _pending = {"session": s, "future": future, "link": link}
        return {"url": _normalize_url(link.verification_uri_complete),
                "code": link.user_code, "expires_in": int(link.expires_in)}
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
    return {"pending": True, "connected": False,
            "url": _normalize_url(link.verification_uri_complete), "code": link.user_code}


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


def search_id(session, artist: str, title: str) -> str | None:
    """Best Tidal track id for (artist, title), or None."""
    want_t, want_a = _norm(title), _norm(artist)
    for q in (f"{_clean(artist)} {_clean(title)}", _clean(title)):
        try:
            results = session.search(q, limit=10)
        except Exception as e:
            log.debug("Tidal search failed (%s): %s", q, e)
            continue
        for tr in _tracks_from(results):
            it_t = _norm(getattr(tr, "name", ""))
            arts = [_norm(getattr(a, "name", "")) for a in (getattr(tr, "artists", []) or [])]
            main = getattr(tr, "artist", None)
            if main is not None:
                arts.append(_norm(getattr(main, "name", "")))
            title_ok = titles_match(title, getattr(tr, "name", ""))
            artist_ok = (not want_a) or any(
                want_a == a or want_a in a or a in want_a for a in arts if a)
            if title_ok and artist_ok:
                tid = getattr(tr, "id", None)
                if tid is not None:
                    return str(tid)
    return None


def create_playlist(name: str, tracks: list) -> dict:
    """tracks: [{'artist','title'}]. Returns {'created','url','added','skipped'}."""
    s = _load_session()
    if s is None:
        raise RuntimeError("Tidal is not authorized -- run: "
                           "python -m audio_recognition.services.tidal login")
    ids, skipped, seen = [], 0, set()
    for t in tracks:
        tid = search_id(s, t.get("artist", ""), t.get("title", ""))
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


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if not configured():
        print("Tidal is disabled. Set AR_TIDAL_ENABLED=1 first.")
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
