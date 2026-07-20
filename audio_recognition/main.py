import asyncio
import datetime
import logging
import signal
import sys
import time
from threading import Thread

from . import autoplaylist, config, corrections, covers, fingerprint, notify, publish, scrobble, state
from .audio.capture import record_and_normalize
from .config import (
    EMA_ALPHA,
    ENRICH_ENABLED,
    SILENCE_RESET_SEGMENTS,
    UNMATCHED_MIN_SEGMENTS,
    EMA_PRUNE_EPSILON,
    EMA_THRESHOLD,
    FLASK_DEBUG,
    FLASK_HOST,
    FLASK_PORT,
    QUIET_BACKOFF_SEC,
)
from .display.image_ops import (
    display_text,
    resize_and_display,
    shutdown_display,
)
from .ema_state import EMAState
from .enrich import enrich_track
from .logging_setup import setup_logging
from .recognize.shazam_client import Track, recognize
from .storage.db import (
    get_album_override,
    ensure_schema,
    get_now_context,
    record_segment,
    save_track,
    update_listened_seconds,
)
from .webapp import create_app

log = logging.getLogger("audio_recognition")
_stop = asyncio.Event()


def state_reset(st) -> None:
    st.reset()


async def _finalize(now: float) -> None:
    """Close out the currently-playing track: record how long it actually ran,
    and scrobble it if it played long enough. Called when a new track locks or
    the room goes quiet."""
    pid, started, tr = state.current_play_meta()
    if not started:
        return
    secs = max(0, int(now - started))
    if pid:
        await asyncio.to_thread(update_listened_seconds, pid, secs)

    # Last.fm's rule of thumb: scrobble at half the track (capped 4 min), never
    # under 30s. With no known duration, fall back to the configured floor.
    if secs < 30:
        return
    dur = tr.get("duration")
    long_enough = secs >= min(dur / 2, 240) if dur else secs >= config.SCROBBLE_MIN_SECONDS
    if long_enough:
        await asyncio.to_thread(scrobble.submit, tr.get("artist"), tr.get("title"),
                                started, tr.get("album"), dur)


def _parse_hours(spec: str):
    try:
        lo, hi = spec.split("-")
        return int(lo), int(hi)
    except Exception:
        return 0, 24


async def _watchdog() -> None:
    """Fire a one-shot alert if the capture input goes silent for too long
    during active hours -- catches an unplugged stereo or a dead USB capture
    before a day of history is lost. Disabled when AR_WATCHDOG_SILENCE_MIN=0."""
    if config.WATCHDOG_SILENCE_MIN <= 0:
        return
    lo, hi = _parse_hours(config.ACTIVE_HOURS)
    limit = config.WATCHDOG_SILENCE_MIN * 60
    alerted = False
    while not _stop.is_set():
        try:
            await asyncio.wait_for(_stop.wait(), timeout=60)
            break  # _stop was set
        except asyncio.TimeoutError:
            pass
        age = state.signal_age()
        hour = datetime.datetime.now().hour
        active = lo <= hour < hi
        if active and age is not None and age > limit:
            if not alerted:
                mins = int(age // 60)
                await asyncio.to_thread(
                    notify.send, "Audio recognizer quiet",
                    f"No audio signal for {mins} min (check device {config.ALSA_DEVICE}).",
                )
                alerted = True
        elif age is not None and age <= limit:
            alerted = False  # re-arm once signal returns


async def loop_pipeline() -> None:
    st = EMAState(alpha=EMA_ALPHA, threshold=EMA_THRESHOLD, prune_epsilon=EMA_PRUNE_EPSILON)

    # Ping-pong buffers: the next segment records into the file we are NOT
    # currently recognizing, so capture overlaps recognition/enrichment instead
    # of the mic sitting idle through the network work. This tightens how fast a
    # song change is caught -- there's no dead gap between segments now.
    buffers = (config.AUDIO_FILE, config.AUDIO_FILE + ".b")
    idx = 0
    miss_run = 0   # consecutive unidentified segments; short runs don't count
    next_capture = asyncio.create_task(record_and_normalize(buffers[idx]))

    while not _stop.is_set():
        try:
            path = await next_capture
        except Exception:
            log.exception("Capture failed; retrying")
            path = None

        # Start the next recording immediately, on the OTHER buffer, before we
        # spend a few seconds recognizing and looking things up. arecord blocks
        # for RECORD_DURATION, so this both paces the loop and overlaps capture.
        idx ^= 1
        next_capture = asyncio.create_task(record_and_normalize(buffers[idx]))

        if not path:
            if state.note_silence() >= SILENCE_RESET_SEGMENTS:
                # The room has gone quiet. Close out the current play, clear Now
                # Playing, and forget the track so the same song replaying later
                # is logged as a new play.
                await _finalize(time.time())
                state.stop()
                await asyncio.to_thread(publish.stopped)
                await asyncio.to_thread(display_text, "Identifying Audio")
                state_reset(st)
            # A failed capture (busy/misnamed device) can return fast; keep the
            # loop from spinning while the next segment records.
            await asyncio.sleep(QUIET_BACKOFF_SEC)
            continue

        state.note_signal()

        # Local recognition first: fingerprint the segment and try to identify
        # it from tracks heard before, so a repeat is resolved from the DB with
        # no Shazam call. On a miss -- or when fpcalc isn't installed -- fall
        # back to Shazam exactly as before. fp_ints is reused below to teach the
        # cache when Shazam is the one that ends up identifying the track.
        fp_ints = None
        track = None
        if fingerprint.available():
            fp_ints = await asyncio.to_thread(fingerprint.compute, path)
            if fp_ints:
                hit = fingerprint.match(fp_ints)
                if hit:
                    meta, score = hit
                    track = Track(
                        key=None, title=meta["title"], artist=meta["artist"],
                        album=meta.get("album"), genre=meta.get("genre"),
                        duration=meta.get("duration"), cover_url=meta.get("cover_url"),
                    )
                    track._local = True
                    log.debug("Local match %.2f: %s", score, track.ident)
        if track is None:
            track = await recognize(path)
        if not track:
            # A track "counts as unmatched" only when audio stays unidentifiable
            # for a sustained run -- brief gaps between/within recognized songs
            # (talking, intros, transitions) don't penalize the match rate.
            miss_run += 1
            if miss_run >= UNMATCHED_MIN_SEGMENTS:
                await asyncio.to_thread(record_segment, False)
            # No trailing sleep -- the next segment is already recording.
            continue

        miss_run = 0
        await asyncio.to_thread(record_segment, True)
        # Apply any learned correction to the raw recognition before it votes or
        # is displayed/saved, so a fixed mis-ID stays fixed.
        track.artist, track.title = corrections.apply(track.artist, track.title)

        if st.update(track.match_key):
            # Close out the play we're leaving (listened_seconds + scrobble).
            now = time.time()
            await _finalize(now)

            # Fill album/genre/duration that Shazam left blank (Last.fm, with a
            # MusicBrainz duration fallback). Off the loop: it does blocking
            # HTTP. Cached, so a song on repeat is only ever looked up once. A
            # local match already carries enriched metadata, so skip it there.
            if ENRICH_ENABLED and not getattr(track, "_local", False):
                await asyncio.to_thread(enrich_track, track)

            # A user-pinned release (chosen in the console) wins over whatever the
            # recognizer attributed the song to.
            _ov = await asyncio.to_thread(get_album_override, track.artist, track.title)
            if _ov and _ov.get("album"):
                track.album = _ov["album"]
                if _ov.get("cover_url"):
                    track.cover_url = _ov["cover_url"]

            log.info("Now playing: %s [dur=%s]", track.ident, track.duration)

            # Context is read BEFORE the insert, so 'plays' is the count before
            # this play and 'play #N' comes out right.
            ctx = await asyncio.to_thread(get_now_context, track.title, track.artist)
            play_id = await asyncio.to_thread(
                save_track,
                track.title,
                track.artist,
                track.album,     # was hardcoded None
                track.genre,     # was hardcoded None
                track.duration,  # was track.get('duration'), a key that never exists
                track.cover_url,
            )
            state.set_now_playing(
                track, play_id,
                {"plays": ctx["plays"] + 1, "last_heard": ctx["last_played"]},
            )

            # Teach the local cache: when Shazam is what identified this track,
            # store its metadata + this segment's fingerprint so the next time
            # it plays we resolve it locally. (Skipped when it was already a
            # local hit -- nothing new to learn.)
            if fp_ints is not None and not getattr(track, "_local", False):
                meta = {"title": track.title, "artist": track.artist,
                        "album": track.album, "genre": track.genre,
                        "duration": track.duration, "cover_url": track.cover_url}
                await asyncio.to_thread(fingerprint.remember, track.match_key, meta, fp_ints)

            # External side effects, all no-ops unless configured.
            await asyncio.to_thread(scrobble.now_playing, track.artist, track.title,
                                    track.album, track.duration)
            await asyncio.to_thread(autoplaylist.add, track.artist, track.title)
            await asyncio.to_thread(publish.now_playing, {
                "title": track.title, "artist": track.artist, "album": track.album,
                "genre": track.genre, "duration": track.duration,
                "cover_url": track.cover_url,
            })

            if track.cover_url:
                # covers.get_bytes resolves from disk, then the DB, then a
                # one-time download (which it also files to both) -- so once the
                # art is cached the physical display never hits the internet
                # either, not just the web archive.
                data = await asyncio.to_thread(covers.get_bytes, track.cover_url)
                if data:
                    await asyncio.to_thread(resize_and_display, data)
                else:
                    await asyncio.to_thread(display_text, track.title)
            else:
                await asyncio.to_thread(display_text, track.title)


def start_flask() -> None:
    app = create_app()
    # use_reloader=False: Werkzeug's reloader installs signal handlers, and
    # app.run(debug=True) from a non-main thread raises
    # "ValueError: signal only works in main thread".
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG,
            use_reloader=False, threaded=True)


async def _run() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop.set)
        except NotImplementedError:  # non-POSIX
            signal.signal(sig, lambda *_: _stop.set())

    pipeline = asyncio.create_task(loop_pipeline())
    watchdog = asyncio.create_task(_watchdog())
    await _stop.wait()
    log.info("Shutting down...")
    for task in (pipeline, watchdog):
        task.cancel()
    for task in (pipeline, watchdog):
        try:
            await task
        except asyncio.CancelledError:
            pass


def main() -> int:
    setup_logging()
    try:
        config.validate()
    except config.ConfigError as e:
        log.error("%s", e)
        return 2

    Thread(target=start_flask, name="flask", daemon=True).start()
    log.info("Web UI on http://%s:%d", FLASK_HOST, FLASK_PORT)

    # Apply migrations and load learned corrections before the loop starts.
    ensure_schema()
    corrections.load()
    if fingerprint.available():
        fingerprint.load()
    from .services import local_library
    if local_library.configured():
        local_library.index()   # warm the folder scan (blocking, one-time)

    display_text("Identifying Audio")
    try:
        asyncio.run(_run())
    finally:
        # The old handle_exit() called sys.exit(0) straight from the signal
        # handler and never reaped feh, so a viewer survived every restart.
        shutdown_display()
    return 0


if __name__ == "__main__":
    sys.exit(main())
