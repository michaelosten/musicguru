"""A local music folder as a library backend, so the want-list, the 'in library'
badge, and M3U export work without Plex.

The folder is scanned once (lazily, cached) and indexed by normalized
title -> {artist -> file path}, mirroring the Plex index. Tags are read with
mutagen; files with no usable tags fall back to their filename.
"""
import logging
import os
import re
import threading
import unicodedata

from .. import config

log = logging.getLogger("audio_recognition.local_library")

_lock = threading.Lock()
_index: dict[str, dict] | None = None   # norm(title) -> { norm(artist): filepath }


def configured() -> bool:
    return bool(config.LOCAL_LIBRARY_PATH and os.path.isdir(config.LOCAL_LIBRARY_PATH))


from ..textmatch import norm as _norm, titles_match


def _read_tags(path: str) -> tuple[str, str]:
    """(artist, title) from tags, falling back to the filename."""
    try:
        from mutagen import File as MutaFile
        m = MutaFile(path, easy=True)
        if m and m.tags:
            artist = (m.tags.get("artist") or m.tags.get("albumartist") or [""])[0]
            title = (m.tags.get("title") or [""])[0]
            if title:
                return artist, title
    except Exception as e:
        log.debug("tag read failed for %s: %s", path, e)
    # filename fallback: "Artist - Title.ext" or just "Title.ext"
    stem = os.path.splitext(os.path.basename(path))[0]
    if " - " in stem:
        a, t = stem.split(" - ", 1)
        return a.strip(), t.strip()
    return "", stem.strip()


def _scan() -> dict:
    exts = tuple(e.strip().lower() for e in config.LOCAL_LIBRARY_EXTS.split(",") if e.strip())
    idx: dict[str, dict] = {}
    root = config.LOCAL_LIBRARY_PATH
    count = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.lower().endswith(exts):
                continue
            path = os.path.join(dirpath, name)
            artist, title = _read_tags(path)
            nt = _norm(title)
            if not nt:
                continue
            idx.setdefault(nt, {}).setdefault(_norm(artist), path)
            count += 1
    log.info("Local library scanned: %d files, %d distinct titles under %s",
             count, len(idx), root)
    return idx


def index() -> dict | None:
    global _index
    if not configured():
        return None
    with _lock:
        if _index is None:
            try:
                _index = _scan()
            except Exception as e:
                log.warning("Local library scan failed: %s", e)
                return None
    return _index


def rescan() -> None:
    global _index
    with _lock:
        _index = None
    index()


def _lookup(artist: str, title: str):
    idx = index()
    if idx is None:
        return None
    arts = idx.get(_norm(title))
    if arts is None and "*" in (title or ""):
        # masked title (e.g. "F**k...") -> scan keys with wildcard matching
        for k, v in idx.items():
            if titles_match(title, k):
                arts = v
                break
    if not arts:
        return None
    wa = _norm(artist)
    if not wa:
        return next(iter(arts.values()))
    for a, path in arts.items():
        if a == wa or wa in a or a in wa:
            return path
    return None


def in_library(artist: str, title: str) -> bool:
    return _lookup(artist, title) is not None


def find_track(artist: str, title: str, album: str = None) -> dict | None:
    """A playable match: {'location': <file path>, 'title', 'artist'} or None."""
    path = _lookup(artist, title)
    if not path:
        return None
    return {"location": path, "title": title, "artist": artist}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    print(f"configured: {configured()}  path: {config.LOCAL_LIBRARY_PATH!r}")
    idx = index()
    print(f"indexed titles: {len(idx) if idx is not None else 'unavailable'}")
    if len(sys.argv) >= 3:
        print(f"find {sys.argv[1]!r} - {sys.argv[2]!r} -> {find_track(sys.argv[1], sys.argv[2])}")
