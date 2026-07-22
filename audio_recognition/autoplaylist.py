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


import threading
import time

_flush_lock = threading.Lock()
_backoff_until: dict[str, float] = {}   # service -> epoch seconds to resume
_consec_fail: dict[str, int] = {}       # service -> consecutive-error count


def _in_backoff(svc: str) -> bool:
    return time.time() < _backoff_until.get(svc, 0)


def _backoff_base(svc: str) -> int:
    # Plex is local: recover quickly. Tidal is rate-limited: ease off harder.
    return 10 if svc == "plex" else config.AUTO_PLAYLIST_TIDAL_BACKOFF_SEC


def _note_error(svc: str) -> None:
    """Escalating backoff after an error response (rate limits, server down)."""
    n = _consec_fail.get(svc, 0) + 1
    _consec_fail[svc] = n
    base = _backoff_base(svc)
    wait = min(base * (2 ** (n - 1)), config.AUTO_PLAYLIST_TIDAL_BACKOFF_MAX_SEC)
    _backoff_until[svc] = time.time() + wait
    log.warning("Auto-playlist: backing off %s for %ds after an error (streak %d)",
                svc, wait, n)


def _is_unavailable(e: Exception) -> bool:
    """A service being unreachable (vs. a genuine per-track failure). These never
    count against a track's attempt budget -- the service will come back."""
    from .plex.client import PlexUnavailable
    if isinstance(e, PlexUnavailable):
        return True
    txt = f"{type(e).__name__}: {e}".lower()
    return any(k in txt for k in (
        "connection", "timeout", "timed out", "unreachable", "refused",
        "reset by peer", "temporarily unavailable", "bad gateway",
        "service unavailable", "not connected", "502", "503", "504",
        "429", "too many requests", "rate limit",
    ))


def _batch_for(svc: str) -> int:
    if svc == "plex":
        return config.AUTO_PLAYLIST_PLEX_BATCH
    return config.AUTO_PLAYLIST_TIDAL_BATCH


def _drain_service(svc: str, name: str) -> tuple:
    """Process one batch for a ready service. Returns
    (added, present, skipped, deferred, errored). Stops the batch on the first
    error so a rate-limited service can back off instead of hammering."""
    rows = db.autoplaylist_queue_pending(config.AUTO_PLAYLIST_MAX_ATTEMPTS,
                                         _batch_for(svc), service=svc)
    added = present = skipped = deferred = 0
    errored = False
    for row in rows:
        key = row["match_key"]
        try:
            status = _add_one(svc, name, row["artist"], row["title"], row.get("album"))
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
            errored = True
            if _is_unavailable(e):
                # Service is down/rate-limited: leave the attempt count alone so a
                # long outage never exhausts a track's retries. It'll be picked up
                # again once the service is back.
                log.warning("Auto-playlist %s unavailable at %s - %s (will retry): %s",
                            svc, row["artist"], row["title"], e)
            else:
                db.autoplaylist_queue_attempt(svc, key)
                log.warning("Auto-playlist %s failed for %s - %s (will retry): %s",
                            svc, row["artist"], row["title"], e)
            break   # back off rather than keep hitting a struggling service
    if not errored and (added or present or skipped):
        _consec_fail[svc] = 0   # clean pass -> reset backoff escalation
    return added, present, skipped, deferred, errored


def flush(limit: int = 25) -> int:
    """Drain queued adds for each ready service. Plex drains a large chunk each
    cycle (as fast as the network allows); Tidal drains a smaller burst and backs
    off when it hits an error. Single-run: overlapping calls no-op. Returns tracks
    added this cycle."""
    if not _flush_lock.acquire(blocking=False):
        return 0
    try:
        name = config.AUTO_PLAYLIST_NAME
        added = present = skipped = deferred = 0
        for svc in _enabled_services():
            if not _service_ready(svc) or _in_backoff(svc):
                continue
            a, p, s, d, errored = _drain_service(svc, name)
            added += a; present += p; skipped += s; deferred += d
            if errored:
                _note_error(svc)
        if added or present or skipped or deferred:
            log.info("Auto-playlist flush: %d added, %d already-in, %d not-found, "
                     "%d deferred; %d still queued", added, present, skipped, deferred,
                     db.autoplaylist_queue_depth())
        return added
    finally:
        _flush_lock.release()


def has_pending() -> bool:
    """Whether any queued item is ready to attempt now (used to pace the worker)."""
    for svc in _enabled_services():
        if _service_ready(svc) and not _in_backoff(svc):
            if db.autoplaylist_queue_pending(config.AUTO_PLAYLIST_MAX_ATTEMPTS, 1, service=svc):
                return True
    return False


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
