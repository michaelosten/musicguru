import asyncio
import logging
import os

from pydub import AudioSegment
from pydub.effects import normalize

from ..config import (
    ALSA_DEVICE,
    AUDIO_FILE,
    CAPTURE_CHANNELS,
    DESIRED_AVG_DBFS,
    NORMALIZATION_HEADROOM_DB,
    PEAK_MARGIN_DB,
    RECORD_DURATION,
    SAMPLE_RATE,
    SILENCE_THRESHOLD_DB,
)

log = logging.getLogger("audio_recognition.audio")


async def _arecord(path: str) -> bool:
    """Record one segment. Returns True on success."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "arecord",
            "-f", "S16_LE",
            "-c", str(CAPTURE_CHANNELS),
            "-r", str(SAMPLE_RATE),
            "-D", ALSA_DEVICE,
            "-d", str(RECORD_DURATION),
            path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.error("arecord not found on PATH -- install alsa-utils")
        return False

    _, stderr = await proc.communicate()

    # The old code ignored the return code entirely. When the ALSA device was
    # busy or misnamed, arecord failed and we either re-decoded the *previous*
    # segment (recognizing the same six seconds over and over) or blew up with
    # an uncaught CouldntDecodeError that killed the whole event loop.
    if proc.returncode != 0:
        msg = (stderr or b"").decode(errors="replace").strip()
        log.error("arecord failed (rc=%s, device=%s): %s", proc.returncode, ALSA_DEVICE, msg)
        return False
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        log.error("arecord produced no data at %s", path)
        return False
    return True


def _process(path: str):
    """Blocking decode + normalize. Run this in a worker thread."""
    try:
        raw = AudioSegment.from_wav(path)
    except Exception as e:
        log.warning("Could not decode %s: %s", path, e)
        return None

    if len(raw) == 0:
        log.warning("Empty recording")
        return None

    # Line-in is typically stereo; recognition (and the fingerprint cache) work
    # on mono, so fold it down before measuring level or exporting.
    if raw.channels > 1:
        raw = raw.set_channels(1)

    log.debug("Recorded dBFS: %.2f", raw.dBFS)
    if raw.dBFS < SILENCE_THRESHOLD_DB:  # -inf for pure silence, compares fine
        log.info("Audio below %.1f dBFS; skipping segment", SILENCE_THRESHOLD_DB)
        return None

    try:
        norm = normalize(raw, headroom=NORMALIZATION_HEADROOM_DB)
        # Peak now sits at -NORMALIZATION_HEADROOM_DB. Clamp the average-level
        # boost so the peak cannot be driven past -PEAK_MARGIN_DB. The old code
        # normalized to -14 dBFS peak and then boosted until the *average* hit
        # -10 dBFS, which pushed peaks well past 0 and hard-clipped int16 --
        # exactly the kind of distortion that ruins a Shazam fingerprint.
        max_boost = max(0.0, NORMALIZATION_HEADROOM_DB - PEAK_MARGIN_DB)
        wanted = DESIRED_AVG_DBFS - norm.dBFS
        boost = max(0.0, min(wanted, max_boost))
        if boost > 0:
            norm = norm + boost
        log.debug("Normalized dBFS: %.2f (boost %.2f dB, cap %.2f dB)", norm.dBFS, boost, max_boost)
        norm.export(path, format="wav")
    except Exception as e:
        log.warning("Normalization failed, using raw audio: %s", e)

    return path


async def record_and_normalize(dest: str | None = None):
    """Record one segment and return its path, or None if unusable.

    dest lets the caller ping-pong between two files so the NEXT segment can
    record while the current one is still being recognized. With a single shared
    AUDIO_FILE that overlap is impossible -- the next arecord would overwrite the
    clip we're mid-match on.
    """
    path = dest or AUDIO_FILE
    if not await _arecord(path):
        return None
    # pydub decode/normalize/export is synchronous CPU+disk work. Off the loop.
    return await asyncio.to_thread(_process, path)
