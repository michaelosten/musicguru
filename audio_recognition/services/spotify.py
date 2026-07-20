"""Create a Spotify playlist from recognized tracks, via spotipy.

Flow: the console links the user to login_url() (Spotify's OAuth consent),
Spotify redirects back to /spotify/callback with a code, handle_callback() trades
it for a token cached on disk, and create_playlist() then searches Spotify for
each track and adds the matches.

Diagnostic:
    python -m audio_recognition.services.spotify "Pink Floyd" "Time"
"""
import logging
import os
import re
import unicodedata

from ..config import (
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_PLAYLIST_PUBLIC,
    SPOTIFY_REDIRECT_URI, SPOTIFY_TOKEN_CACHE,
)

log = logging.getLogger("audio_recognition.spotify")

_SCOPE = "playlist-modify-private playlist-modify-public"


def configured() -> bool:
    return bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET and SPOTIFY_REDIRECT_URI)


from ..textmatch import norm as _norm, query_title as _clean, titles_match


def _oauth():
    from spotipy.oauth2 import SpotifyOAuth
    d = os.path.dirname(SPOTIFY_TOKEN_CACHE)
    if d:
        os.makedirs(d, exist_ok=True)
    return SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI, scope=_SCOPE,
        cache_path=SPOTIFY_TOKEN_CACHE, open_browser=False,
    )


def login_url() -> str | None:
    """The Spotify consent URL to send the user to (one-time authorization)."""
    if not configured():
        return None
    try:
        return _oauth().get_authorize_url()
    except Exception as e:
        log.warning("Spotify authorize URL failed: %s", e)
        return None


def handle_callback(code: str) -> bool:
    """Exchange the ?code= from Spotify's redirect for a cached token."""
    if not configured() or not code:
        return False
    try:
        _oauth().get_access_token(code, as_dict=False, check_cache=False)
        return True
    except Exception as e:
        log.warning("Spotify token exchange failed: %s", e)
        return False


def connected() -> bool:
    """True once a token has been authorized and cached (refreshable)."""
    if not configured():
        return False
    try:
        oauth = _oauth()
        tok = oauth.cache_handler.get_cached_token()
        return bool(tok and oauth.validate_token(tok))
    except Exception:
        return False


def _client():
    from spotipy import Spotify
    oauth = _oauth()
    tok = oauth.cache_handler.get_cached_token()
    if not tok:
        return None
    return Spotify(auth_manager=oauth)


def search_uri(sp, artist: str, title: str, album: str = None) -> str | None:
    """Best Spotify track URI for (artist, title), preferring a given album."""
    want_t, want_a, want_al = _norm(title), _norm(artist), (_norm(album) if album else "")
    items = []
    for q in (f'track:{_clean(title)} artist:{_clean(artist)}',
              f'{_clean(artist)} {_clean(title)}'):
        try:
            items = sp.search(q=q, type="track", limit=8)["tracks"]["items"]
        except Exception as e:
            log.debug("Spotify search failed (%s): %s", q, e)
            items = []
        if items:
            break
    matches = []
    for it in items:
        arts = [_norm(a.get("name", "")) for a in it.get("artists", [])]
        title_ok = titles_match(title, it.get("name", ""))
        artist_ok = (not want_a) or any(want_a == a or want_a in a or a in want_a for a in arts)
        if title_ok and artist_ok:
            matches.append(it)
    if not matches:
        return None
    if want_al:
        for it in matches:
            it_al = _norm((it.get("album") or {}).get("name", ""))
            if it_al and (it_al == want_al or want_al in it_al or it_al in want_al):
                return it.get("uri")   # pinned album wins
    return matches[0].get("uri")


def create_playlist(name: str, tracks: list) -> dict:
    """tracks: [{'artist','title'}]. Returns {'created','url','added','skipped'}."""
    sp = _client()
    if sp is None:
        raise RuntimeError("Spotify is not connected -- authorize it on the Config page.")
    uid = sp.me()["id"]

    uris, skipped = [], 0
    seen = set()
    for t in tracks:
        uri = search_uri(sp, t.get("artist", ""), t.get("title", ""), t.get("album"))
        if uri and uri not in seen:
            uris.append(uri)
            seen.add(uri)
        elif not uri:
            skipped += 1
    if not uris:
        raise RuntimeError("none of these tracks were found on Spotify")

    pl = sp.user_playlist_create(uid, name, public=SPOTIFY_PLAYLIST_PUBLIC,
                                 description="Created by musicguru")
    for i in range(0, len(uris), 100):
        sp.playlist_add_items(pl["id"], uris[i:i + 100])
    return {"created": True, "url": pl["external_urls"]["spotify"],
            "added": len(uris), "skipped": skipped}


_pl_cache: dict[str, str] = {}   # playlist name -> id (auto-playlist)


def _find_or_create_playlist(sp, name: str) -> str:
    if name in _pl_cache:
        return _pl_cache[name]
    uid = sp.me()["id"]
    page = sp.current_user_playlists(limit=50)
    while page:
        for pl in page.get("items", []):
            if pl and pl.get("name") == name and pl.get("owner", {}).get("id") == uid:
                _pl_cache[name] = pl["id"]
                return pl["id"]
        page = sp.next(page) if page.get("next") else None
    pl = sp.user_playlist_create(uid, name, public=SPOTIFY_PLAYLIST_PUBLIC,
                                 description="Auto-generated by musicguru")
    _pl_cache[name] = pl["id"]
    return pl["id"]


def add_to_named_playlist(name: str, artist: str, title: str, album: str = None) -> bool:
    """Append one track to the named playlist (create it if needed). Returns True
    if added, False if the track wasn't found on Spotify."""
    sp = _client()
    if sp is None:
        raise RuntimeError("Spotify not connected")
    uri = search_uri(sp, artist, title, album)
    if not uri:
        return False
    sp.playlist_add_items(_find_or_create_playlist(sp, name), [uri])
    return True


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    print(f"configured: {configured()}  connected: {connected()}")
    if not configured():
        print("Set AR_SPOTIFY_CLIENT_ID / AR_SPOTIFY_CLIENT_SECRET / "
              "AR_SPOTIFY_REDIRECT_URI, then authorize on the Config page.")
        sys.exit(1)
    if not connected():
        print("Not authorized yet. Open this URL, approve, and let it redirect back:")
        print("  " + (login_url() or "<none>"))
        sys.exit(1)
    sp = _client()
    print("me:", sp.me().get("id"))
    if len(sys.argv) >= 3:
        uri = search_uri(sp, sys.argv[1], sys.argv[2])
        print(f"search {sys.argv[1]!r} - {sys.argv[2]!r} -> {uri}")
