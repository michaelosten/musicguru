"""Continuously append every newly heard track to a playlist on each enabled
external service (Spotify, Tidal, Plex).

Called once per new now-playing track. Best-effort and self-contained: a service
being down or a track being absent never disrupts the recognition pipeline. Each
distinct track is added at most once per service, deduped in the DB so the set
survives restarts.
"""
import logging

from . import config, textmatch
from .plex import client as plex
from .services import spotify, tidal
from .storage import db

log = logging.getLogger("audio_recognition.autoplaylist")


def targets() -> list[str]:
    """Enabled + usable (connected) services, for status/UX."""
    out = []
    if config.AUTO_PLAYLIST_SPOTIFY and spotify.configured() and spotify.connected():
        out.append("spotify")
    if config.AUTO_PLAYLIST_TIDAL and tidal.configured() and tidal.connected():
        out.append("tidal")
    if config.AUTO_PLAYLIST_PLEX and plex.configured():
        out.append("plex")
    return out


def enabled() -> bool:
    return bool(targets())


def _add_one(service: str, name: str, artist: str, title: str, album: str = None) -> bool:
    if service == "spotify":
        return spotify.add_to_named_playlist(name, artist, title, album)
    if service == "tidal":
        return tidal.add_to_named_playlist(name, artist, title, album)
    if service == "plex":
        m = plex.find_track(artist, title, album)
        if m and m.get("rating_key"):
            plex.create_or_append_playlist(name, [m["rating_key"]])
            return True
        return False
    return False


def add(artist: str, title: str) -> None:
    """Add a newly heard track to the auto-playlist on each enabled service."""
    tgts = targets()
    if not tgts:
        return
    name = config.AUTO_PLAYLIST_NAME
    ov = db.get_album_override(artist, title)
    album = ov["album"] if ov else None
    key = f"{textmatch.norm(artist)}|{textmatch.norm(title)}"
    for svc in tgts:
        try:
            if db.autoplaylist_seen(svc, key):
                continue
            added = _add_one(svc, name, artist, title, album)
            # Mark as handled whether or not it was found, so we don't re-search
            # a not-on-service track every time it plays. Only a hard error
            # (service down) leaves it unmarked, so it retries later.
            db.autoplaylist_mark(svc, key)
            if added:
                log.info("Auto-playlist: added %s - %s to %s", artist, title, svc)
        except Exception as e:
            log.warning("Auto-playlist %s failed for %s - %s: %s", svc, artist, title, e)
