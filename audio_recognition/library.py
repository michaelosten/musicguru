"""Unified music-library backend so the console works with Plex OR a local music
folder OR neither. Plex wins when configured; a local folder is the fallback.

Used by the want-list, the 'in library' badge, and M3U export.
"""
from .plex import client as plex
from .services import local_library as local


def backend() -> str | None:
    if plex.configured():
        return "plex"
    if local.configured():
        return "local"
    return None


def configured() -> bool:
    return backend() is not None


def name() -> str:
    return {"plex": "Plex", "local": "a local folder"}.get(backend(), "")


def in_library(artist: str, title: str) -> bool:
    b = backend()
    if b == "plex":
        return plex.in_library(artist, title)
    if b == "local":
        return local.in_library(artist, title)
    return False


def presence_batch(pairs) -> dict:
    b = backend()
    if b == "plex":
        return plex.presence_batch(pairs)
    if b == "local":
        return {p: local.in_library(p[0], p[1]) for p in pairs}
    return {p: False for p in pairs}


def resolve(artist: str, title: str) -> dict | None:
    """A playable match for M3U export, preferring a user-pinned release. Plex ->
    {'backend':'plex','part_key','duration'}; local -> {'backend':'local','location'}."""
    from .storage import db
    ov = db.get_album_override(artist, title)
    album = ov["album"] if ov else None
    b = backend()
    if b == "plex":
        try:
            m = plex.find_track(artist, title, album)
        except Exception:
            m = None   # Plex unreachable -> no playable match right now
        if m:
            return {"backend": "plex", "part_key": m.get("part_key"),
                    "duration": m.get("duration")}
    elif b == "local":
        m = local.find_track(artist, title, album)
        if m:
            return {"backend": "local", "location": m["location"], "duration": None}
    return None
