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


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s)
    return re.sub(r"[^0-9a-z]+", "", s.lower())


def _clean(s: str) -> str:
    return re.sub(r"\(.*?\)|\[.*?\]", "", s or "").strip()


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


def search_uri(sp, artist: str, title: str) -> str | None:
    """Best Spotify track URI for (artist, title), or None."""
    want_t, want_a = _norm(title), _norm(artist)
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
    # exact-ish: title matches and one artist matches
    for it in items:
        it_t = _norm(it.get("name", ""))
        arts = [_norm(a.get("name", "")) for a in it.get("artists", [])]
        title_ok = it_t == want_t or want_t in it_t or it_t in want_t
        artist_ok = (not want_a) or any(want_a == a or want_a in a or a in want_a for a in arts)
        if title_ok and artist_ok:
            return it.get("uri")
    return None


def create_playlist(name: str, tracks: list) -> dict:
    """tracks: [{'artist','title'}]. Returns {'created','url','added','skipped'}."""
    sp = _client()
    if sp is None:
        raise RuntimeError("Spotify is not connected -- authorize it on the Config page.")
    uid = sp.me()["id"]

    uris, skipped = [], 0
    seen = set()
    for t in tracks:
        uri = search_uri(sp, t.get("artist", ""), t.get("title", ""))
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
