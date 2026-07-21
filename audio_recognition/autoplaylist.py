"""Append every newly heard track to a playlist on each enabled service
(Spotify, Tidal, Plex), reliably.

Design: hearing a track and adding it to a service are decoupled. On a new play
the track is *queued* per enabled service (a fast DB write that can't fail the
pipeline). A background worker then flushes the queue, retrying until each add
succeeds -- so a service that's briefly down, disconnected, or rate-limited no
longer silently drops tracks. Successful/handled tracks move to auto_playlist_log
(deduped, survives restarts); the queue only holds outstanding work.
"""
import logging

from . import config, textmatch
from .plex import client as plex
from .services import spotify, tidal
from .storage import db

log = logging.getLogger("audio_recognition.autoplaylist")


def _key(artist: str, title: str) -> str:
    return f"{textmatch.norm(artist)}|{textmatch.norm(title)}"


def _enabled_services() -> list[str]:
    """Services toggled on and configured -- used for QUEUEING (connection is
    checked later, at flush time, so a disconnect doesn't drop the track)."""
    out = []
    if config.AUTO_PLAYLIST_SPOTIFY and spotify.configured():
        out.append("spotify")
    if config.AUTO_PLAYLIST_TIDAL and tidal.configured():
        out.append("tidal")
    if config.AUTO_PLAYLIST_PLEX and plex.configured():
        out.append("plex")
    return out


def _service_ready(svc: str) -> bool:
    if svc == "spotify":
        return spotify.connected()
    if svc == "tidal":
        return tidal.connected()
    if svc == "plex":
        return plex.configured()
    return False


def targets() -> list[str]:
    """Enabled + currently-usable services, for status/UX."""
    return [s for s in _enabled_services() if _service_ready(s)]


def enabled() -> bool:
    return bool(_enabled_services())


def _add_one(service: str, name: str, artist: str, title: str, album: str = None) -> str:
    """Returns 'added', 'present' (already in the playlist), or 'absent' (not
    found on the service)."""
    if service == "spotify":
        return spotify.add_to_named_playlist(name, artist, title, album)
    if service == "tidal":
        return tidal.add_to_named_playlist(name, artist, title, album)
    if service == "plex":
        rk = plex.match_rating_key(artist, title, album)
        if not rk:
            return "absent"
        res = plex.create_or_append_playlist(name, [rk])
        return "added" if res.get("added", 1) else "present"
    return "absent"


def enqueue(artist: str, title: str, album: str = None) -> None:
    """Queue a newly heard track for each enabled service (skips ones already
    handled). Cheap; the flush worker does the actual adding."""
    svcs = _enabled_services()
    if not svcs:
        return
    key = _key(artist, title)
    ov = db.get_album_override(artist, title)
    al = ov["album"] if ov else album
    for svc in svcs:
        if db.autoplaylist_seen(svc, key):
            continue
        db.autoplaylist_enqueue(svc, key, artist, title, al)


def flush(limit: int = 25) -> int:
    """Attempt outstanding queued adds for services that are ready. Returns the
    number of tracks added. Safe to call often; a no-op when the queue is empty."""
    rows = db.autoplaylist_queue_pending(config.AUTO_PLAYLIST_MAX_ATTEMPTS, limit)
    if not rows:
        return 0
    ready = {s: _service_ready(s) for s in set(r["service"] for r in rows)}
    name = config.AUTO_PLAYLIST_NAME
    added = present = skipped = deferred = 0
    for row in rows:
        svc, key = row["service"], row["match_key"]
        if not ready.get(svc):
            continue   # service not usable right now; leave for a later flush
        try:
            status = _add_one(svc, name, row["artist"], row["title"], row.get("album"))
            # added / present / absent are all "handled" -> stop retrying.
            db.autoplaylist_mark(svc, key)
            db.autoplaylist_queue_remove(svc, key)
            if status == "added":
                added += 1
                log.info("Auto-playlist: added %s - %s to %s",
                         row["artist"], row["title"], svc)
            elif status == "present":
                present += 1
                log.info("Auto-playlist: %s - %s already in %s playlist (skipping)",
                         row["artist"], row["title"], svc)
            else:
                skipped += 1
                log.info("Auto-playlist: %s - %s not found on %s (skipping)",
                         row["artist"], row["title"], svc)
        except Exception as e:
            deferred += 1
            db.autoplaylist_queue_attempt(svc, key)   # transient; retry later
            log.warning("Auto-playlist %s failed for %s - %s (will retry): %s",
                        svc, row["artist"], row["title"], e)
    if added or present or skipped or deferred:
        log.info("Auto-playlist flush: %d added, %d already-in, %d not-found, "
                 "%d deferred; %d still queued", added, present, skipped, deferred,
                 db.autoplaylist_queue_depth())
    return added


def note_played(artist: str, title: str, album: str = None) -> None:
    """Called on each new now-playing track: queue it, then try to add promptly."""
    enqueue(artist, title, album)
    flush()


def backfill() -> int:
    """Queue every distinct track already in the archive (that isn't handled yet)
    for the enabled services. Returns how many (service, track) items were queued."""
    svcs = _enabled_services()
    if not svcs:
        return 0
    queued = 0
    for r in db.distinct_tracks_for_backfill():
        key = _key(r["artist"], r["title"])
        for svc in svcs:
            if db.autoplaylist_seen(svc, key):
                continue
            db.autoplaylist_enqueue(svc, key, r["artist"], r["title"], r.get("album"))
            queued += 1
    log.info("Auto-playlist backfill queued %d item(s)", queued)
    return queued
