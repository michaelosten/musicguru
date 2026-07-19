import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime

import mysql.connector
from mysql.connector import pooling

from ..config import DB_CONFIG, DB_POOL_SIZE, DB_SPOOL_FILE, DB_TIMES_ARE_UTC
from ..utils.timezone import utc_to_local_str

log = logging.getLogger("audio_recognition.storage")

_pool: pooling.MySQLConnectionPool | None = None

SORTS = {
    "recent": "last_played DESC",
    "oldest": "last_played ASC",
    "plays":  "plays DESC, last_played DESC",
    "artist": "artist ASC, album ASC, title ASC",
    "title":  "title ASC",
    "album":  "album ASC, title ASC",
}

# Strips a trailing "(2024 Remaster)" / "[Live]" so variants of the same song
# collapse into one row instead of splitting their play counts.
_BASE_TITLE = r"REGEXP_REPLACE(title, ' *[\\(\\[].*$', '')"


def _get_pool() -> pooling.MySQLConnectionPool:
    global _pool
    if _pool is None:
        _pool = pooling.MySQLConnectionPool(
            pool_name="audio_recognition",
            pool_size=DB_POOL_SIZE,
            pool_reset_session=True,
            **DB_CONFIG,
        )
    return _pool


@contextmanager
def _cursor(dictionary: bool = False):
    """Always returns the connection to the pool, on any exception."""
    conn = cur = None
    try:
        conn = _get_pool().get_connection()
        cur = conn.cursor(dictionary=dictionary)
        yield conn, cur
    finally:
        for obj in (cur, conn):
            if obj is not None:
                try:
                    obj.close()
                except mysql.connector.Error:
                    pass


def _local_offset_minutes() -> int:
    """Rows are stored UTC; shift them for date/hour bucketing in the UI."""
    if not DB_TIMES_ARE_UTC:
        return 0
    off = datetime.now().astimezone().utcoffset()
    return int(off.total_seconds() // 60) if off else 0


def _filters(q, genre, date_from, date_to, after=None, before=None):
    where, params = [], []
    if q:
        like = f"%{q}%"
        where.append("(title LIKE %s OR artist LIKE %s OR album LIKE %s)")
        params += [like, like, like]
    if genre:
        where.append("genre = %s")
        params.append(genre)
    if date_from:
        where.append("recognized_at >= %s")
        params.append(date_from)
    if date_to:
        where.append("recognized_at < %s + INTERVAL 1 DAY")
        params.append(date_to)
    # after/before are precise half-open UTC datetime bounds (used by the
    # Today/Yesterday/range quick filters, computed in the browser's local tz).
    if after:
        where.append("recognized_at >= %s")
        params.append(after)
    if before:
        where.append("recognized_at < %s")
        params.append(before)
    return ("WHERE " + " AND ".join(where)) if where else "", params


# --- writes --------------------------------------------------------------

def _insert(row: dict, ts: str | None = None) -> tuple[bool, int | None]:
    """One insert. ts (a 'YYYY-MM-DD HH:MM:SS' UTC string) is used when
    replaying spooled rows so they keep their original time; live inserts pass
    None and get UTC_TIMESTAMP()."""
    when = "%s" if ts else "UTC_TIMESTAMP()"
    params = [row["title"], row["artist"], row["album"], row["genre"],
              row["duration"], row["cover_url"]]
    if ts:
        params.append(ts)
    try:
        with _cursor() as (conn, cur):
            cur.execute(
                "INSERT INTO recognized_songs "
                "(title, artist, album, genre, duration, cover_url, recognized_at) "
                f"VALUES (%s, %s, %s, %s, %s, %s, {when})",
                tuple(params),
            )
            conn.commit()
            return True, cur.lastrowid
    except mysql.connector.Error as e:
        log.warning("DB insert failed: %s", e)
        return False, None


def _spool(row: dict) -> None:
    """Persist a play we couldn't write, to replay once the DB is back."""
    try:
        row = dict(row)
        row["ts"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        parent = os.path.dirname(DB_SPOOL_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(DB_SPOOL_FILE, "a") as f:
            f.write(json.dumps(row) + "\n")
        log.warning("Play spooled (DB unavailable): %s - %s", row.get("artist"), row.get("title"))
    except OSError as e:
        log.error("Could not spool play: %s", e)


def _flush_spool() -> None:
    """Replay spooled plays in order. Stops at the first row that still fails,
    keeping it and everything after it for the next attempt."""
    if not (os.path.exists(DB_SPOOL_FILE) and os.path.getsize(DB_SPOOL_FILE) > 0):
        return
    try:
        with open(DB_SPOOL_FILE) as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return

    kept, stuck = [], False
    for ln in lines:
        if stuck:
            kept.append(ln)
            continue
        try:
            row = json.loads(ln)
        except ValueError:
            continue  # drop a corrupt line
        ok, _ = _insert(row, ts=row.get("ts"))
        if not ok:
            stuck = True
            kept.append(ln)

    try:
        if kept:
            with open(DB_SPOOL_FILE, "w") as f:
                f.write("\n".join(kept) + "\n")
        else:
            os.remove(DB_SPOOL_FILE)
        replayed = len(lines) - len(kept)
        if replayed:
            log.info("Replayed %d spooled play(s).", replayed)
    except OSError:
        pass


def save_track(title, artist, album=None, genre=None, duration=None, cover_url=None) -> int | None:
    """Insert a play, replaying any spooled rows first. Returns the new row id,
    or None on failure (the play is then spooled for later)."""
    row = {"title": title, "artist": artist, "album": album,
           "genre": genre, "duration": duration, "cover_url": cover_url}
    _flush_spool()
    ok, new_id = _insert(row)
    if not ok:
        _spool(row)
        return None
    return new_id


def update_listened_seconds(play_id, seconds) -> None:
    """Record how long a play actually ran (set when the track is replaced)."""
    if not play_id or seconds is None:
        return
    try:
        with _cursor() as (conn, cur):
            cur.execute(
                "UPDATE recognized_songs SET listened_seconds = %s WHERE id = %s",
                (int(seconds), int(play_id)),
            )
            conn.commit()
    except (mysql.connector.Error, ValueError) as e:
        log.warning("update_listened_seconds failed: %s", e)


def record_segment(matched: bool) -> None:
    """Count one recognition segment as matched or missed, bucketed by local
    day+hour, for the recognition-rate stat. Bounded to 24 rows per day."""
    col = "matched" if matched else "missed"
    off = _local_offset_minutes()
    try:
        with _cursor() as (conn, cur):
            cur.execute(
                f"INSERT INTO segment_counts (day, hour, {col}) VALUES "
                f"(DATE(UTC_TIMESTAMP() + INTERVAL %s MINUTE), "
                f"HOUR(UTC_TIMESTAMP() + INTERVAL %s MINUTE), 1) "
                f"ON DUPLICATE KEY UPDATE {col} = {col} + 1",
                (off, off),
            )
            conn.commit()
    except mysql.connector.Error as e:
        log.debug("record_segment failed: %s", e)


def delete_play(play_id: int) -> int:
    """Remove a single play from the history."""
    try:
        with _cursor() as (conn, cur):
            cur.execute("DELETE FROM recognized_songs WHERE id = %s", (int(play_id),))
            conn.commit()
            return cur.rowcount
    except (mysql.connector.Error, ValueError) as e:
        log.warning("delete_play failed: %s", e)
        return 0


def forget_track(title: str, artist: str) -> int:
    """Remove every play of a track -- for purging misrecognitions."""
    try:
        with _cursor() as (conn, cur):
            cur.execute(
                "DELETE FROM recognized_songs WHERE title = %s AND artist = %s",
                (title, artist),
            )
            conn.commit()
            return cur.rowcount
    except mysql.connector.Error as e:
        log.warning("forget_track failed: %s", e)
        return 0


# --- schema / migrations -------------------------------------------------

def ensure_schema() -> None:
    """Idempotent migrations, safe to run at every startup. MySQL lacks
    ADD COLUMN IF NOT EXISTS, so the column is checked against
    information_schema first."""
    try:
        with _cursor() as (conn, cur):
            cur.execute("SELECT DATABASE()")
            dbname = (cur.fetchone() or [None])[0]
            cur.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = %s "
                "AND table_name = 'recognized_songs' AND column_name = 'listened_seconds'",
                (dbname,),
            )
            if not cur.fetchone():
                cur.execute("ALTER TABLE recognized_songs ADD COLUMN listened_seconds INT NULL")
                log.info("Added recognized_songs.listened_seconds")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS corrections ("
                " raw_key VARCHAR(255) NOT NULL PRIMARY KEY,"
                " artist VARCHAR(512) NOT NULL,"
                " title VARCHAR(512) NOT NULL,"
                " created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS segment_counts ("
                " day DATE NOT NULL, hour TINYINT NOT NULL,"
                " matched INT NOT NULL DEFAULT 0, missed INT NOT NULL DEFAULT 0,"
                " PRIMARY KEY (day, hour))"
            )
            # Local-recognition cache: canonical metadata per identity, plus the
            # Chromaprint fingerprints that map an unknown segment back to it.
            cur.execute(
                "CREATE TABLE IF NOT EXISTS known_tracks ("
                " match_key VARCHAR(255) NOT NULL PRIMARY KEY,"
                " title VARCHAR(512), artist VARCHAR(512), album VARCHAR(512),"
                " genre VARCHAR(255), duration INT, cover_url TEXT,"
                " updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                " ON UPDATE CURRENT_TIMESTAMP)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS fingerprints ("
                " id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,"
                " match_key VARCHAR(255) NOT NULL,"
                " fp MEDIUMTEXT NOT NULL,"
                " created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                " KEY k_match (match_key))"
            )
            # Cover art stored IN the database so it survives a lost disk cache
            # and never needs re-fetching from the internet. Keyed by a hash of
            # the source URL, so plays that share art share one blob.
            cur.execute(
                "CREATE TABLE IF NOT EXISTS cover_blobs ("
                " cover_key CHAR(40) NOT NULL PRIMARY KEY,"
                " mime VARCHAR(32) NOT NULL DEFAULT 'image/jpeg',"
                " data MEDIUMBLOB NOT NULL,"
                " created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            conn.commit()
        log.info("Schema ensured.")
    except mysql.connector.Error as e:
        log.error("ensure_schema failed: %s", e)


# --- corrections ---------------------------------------------------------

def load_corrections() -> dict:
    try:
        with _cursor(dictionary=True) as (_c, cur):
            cur.execute("SELECT raw_key, artist, title FROM corrections")
            return {r["raw_key"]: (r["artist"], r["title"]) for r in cur.fetchall()}
    except mysql.connector.Error as e:
        log.warning("load_corrections failed: %s", e)
        return {}


def save_correction(raw_key, artist, title) -> bool:
    try:
        with _cursor() as (conn, cur):
            cur.execute(
                "INSERT INTO corrections (raw_key, artist, title) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE artist = VALUES(artist), title = VALUES(title)",
                (raw_key, artist, title),
            )
            conn.commit()
        return True
    except mysql.connector.Error as e:
        log.warning("save_correction failed: %s", e)
        return False


def relabel(old_title, old_artist, new_title, new_artist) -> int:
    """Rewrite existing plays that match the mis-recognized pair."""
    try:
        with _cursor() as (conn, cur):
            cur.execute(
                "UPDATE recognized_songs SET title = %s, artist = %s "
                "WHERE title = %s AND artist = %s",
                (new_title, new_artist, old_title, old_artist),
            )
            conn.commit()
            return cur.rowcount
    except mysql.connector.Error as e:
        log.warning("relabel failed: %s", e)
        return 0


# --- now-playing context / recognition rate ------------------------------

def get_now_context(title, artist) -> dict:
    """Play count and previous last-heard for a track. Call BEFORE inserting the
    current play, so 'plays' is the count *before* this one."""
    try:
        with _cursor(dictionary=True) as (_c, cur):
            cur.execute(
                "SELECT COUNT(*) plays, MAX(recognized_at) last FROM recognized_songs "
                "WHERE title = %s AND artist = %s",
                (title, artist),
            )
            r = cur.fetchone() or {}
            return {"plays": int(r.get("plays") or 0),
                    "last_played": utc_to_local_str(r.get("last"))}
    except mysql.connector.Error:
        return {"plays": 0, "last_played": ""}


def get_recognition_rate(days: int = 7) -> dict:
    try:
        with _cursor(dictionary=True) as (_c, cur):
            cur.execute(
                "SELECT COALESCE(SUM(matched), 0) m, COALESCE(SUM(missed), 0) x "
                "FROM segment_counts WHERE day >= (CURDATE() - INTERVAL %s DAY)",
                (days,),
            )
            r = cur.fetchone() or {}
            m, x = int(r.get("m") or 0), int(r.get("x") or 0)
            total = m + x
            return {"matched": m, "missed": x, "rate": (m / total) if total else None}
    except mysql.connector.Error:
        return {"matched": 0, "missed": 0, "rate": None}


def get_metrics() -> dict:
    try:
        with _cursor(dictionary=True) as (_c, cur):
            cur.execute(
                "SELECT COUNT(*) plays, COUNT(DISTINCT title, artist) tracks "
                "FROM recognized_songs"
            )
            return cur.fetchone() or {}
    except mysql.connector.Error:
        return {}


def get_distinct_tracks(cap: int = 600) -> list[dict]:
    """One representative row per (title, artist), most-played first -- the pool
    the want-list checks against Plex."""
    try:
        with _cursor(dictionary=True) as (_c, cur):
            cur.execute(
                "SELECT MAX(id) id, MIN(title) title, artist, MAX(cover_url) cover_url, "
                "COUNT(*) plays FROM recognized_songs "
                "GROUP BY title, artist ORDER BY plays DESC LIMIT %s",
                (cap,),
            )
            return cur.fetchall()
    except mysql.connector.Error as e:
        log.error("get_distinct_tracks error: %s", e)
        return []


# --- local recognition cache ---------------------------------------------

def upsert_known_track(match_key: str, meta: dict) -> None:
    try:
        with _cursor() as (conn, cur):
            cur.execute(
                "INSERT INTO known_tracks "
                "(match_key, title, artist, album, genre, duration, cover_url) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE title=VALUES(title), artist=VALUES(artist), "
                "album=VALUES(album), genre=VALUES(genre), duration=VALUES(duration), "
                "cover_url=VALUES(cover_url)",
                (match_key, meta.get("title"), meta.get("artist"), meta.get("album"),
                 meta.get("genre"), meta.get("duration"), meta.get("cover_url")),
            )
            conn.commit()
    except mysql.connector.Error as e:
        log.warning("upsert_known_track failed: %s", e)


def load_known_tracks() -> dict:
    try:
        with _cursor(dictionary=True) as (_c, cur):
            cur.execute("SELECT match_key, title, artist, album, genre, duration, "
                        "cover_url FROM known_tracks")
            out = {}
            for r in cur.fetchall():
                key = r.pop("match_key")
                out[key] = r
            return out
    except mysql.connector.Error as e:
        log.warning("load_known_tracks failed: %s", e)
        return {}


def add_fingerprint(match_key: str, ints: list[int], cap: int) -> int | None:
    """Store one fingerprint (as comma-joined ints) and evict the oldest beyond
    `cap` for this track. Returns the new row id."""
    fp_text = ",".join(str(v) for v in ints)
    try:
        with _cursor() as (conn, cur):
            cur.execute("INSERT INTO fingerprints (match_key, fp) VALUES (%s,%s)",
                        (match_key, fp_text))
            new_id = cur.lastrowid
            cur.execute(
                "DELETE FROM fingerprints WHERE match_key=%s AND id NOT IN "
                "(SELECT id FROM (SELECT id FROM fingerprints WHERE match_key=%s "
                " ORDER BY id DESC LIMIT %s) keep)",
                (match_key, match_key, cap),
            )
            conn.commit()
            return new_id
    except mysql.connector.Error as e:
        log.warning("add_fingerprint failed: %s", e)
        return None


def load_fingerprints() -> list[tuple]:
    try:
        with _cursor() as (_c, cur):
            cur.execute("SELECT id, match_key, fp FROM fingerprints")
            out = []
            for fp_id, key, fp_text in cur.fetchall():
                try:
                    ints = [int(x) for x in fp_text.split(",") if x]
                except (ValueError, AttributeError):
                    continue
                out.append((fp_id, key, ints))
            return out
    except mysql.connector.Error as e:
        log.warning("load_fingerprints failed: %s", e)
        return []


# --- cover blobs (art stored in the DB) ----------------------------------

def save_cover_blob(cover_key: str, mime: str, data: bytes) -> None:
    try:
        with _cursor() as (conn, cur):
            cur.execute(
                "INSERT INTO cover_blobs (cover_key, mime, data) VALUES (%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE mime=VALUES(mime), data=VALUES(data)",
                (cover_key, mime, data),
            )
            conn.commit()
    except mysql.connector.Error as e:
        log.warning("save_cover_blob failed: %s", e)


def get_cover_blob(cover_key: str) -> tuple[str, bytes] | None:
    try:
        with _cursor() as (_c, cur):
            cur.execute("SELECT mime, data FROM cover_blobs WHERE cover_key=%s", (cover_key,))
            row = cur.fetchone()
            return (row[0], row[1]) if row else None
    except mysql.connector.Error as e:
        log.warning("get_cover_blob failed: %s", e)
        return None


def has_cover_blob(cover_key: str) -> bool:
    try:
        with _cursor() as (_c, cur):
            cur.execute("SELECT 1 FROM cover_blobs WHERE cover_key=%s", (cover_key,))
            return cur.fetchone() is not None
    except mysql.connector.Error:
        return False


# --- reads ---------------------------------------------------------------

def get_archive(offset=0, limit=20, q=None, genre=None, sort="recent",
                merge_variants=False, date_from=None, date_to=None,
                after=None, before=None) -> list[dict]:
    where, params = _filters(q, genre, date_from, date_to, after, before)
    order = SORTS.get(sort, SORTS["recent"])
    group = f"{_BASE_TITLE}, artist" if merge_variants else "title, artist"

    sql = f"""
        SELECT MAX(id)                  AS id,
               MIN(title)               AS title,
               artist,
               MAX(album)               AS album,
               MAX(genre)               AS genre,
               MAX(duration)            AS duration,
               MAX(cover_url)           AS cover_url,
               COUNT(*)                 AS plays,
               MAX(recognized_at)       AS last_played,
               MIN(recognized_at)       AS first_played
        FROM recognized_songs
        {where}
        GROUP BY {group}
        ORDER BY {order}
        LIMIT %s OFFSET %s
    """
    try:
        with _cursor(dictionary=True) as (_c, cur):
            cur.execute(sql, tuple(params) + (limit, offset))
            rows = cur.fetchall()
    except mysql.connector.Error as e:
        if merge_variants:
            log.warning("merge_variants needs MySQL 8 (REGEXP_REPLACE): %s", e)
            return get_archive(offset, limit, q, genre, sort, False, date_from, date_to, after, before)
        log.error("get_archive error: %s", e)
        return []

    for r in rows:
        r["last_played"] = utc_to_local_str(r["last_played"])
        r["first_played"] = utc_to_local_str(r["first_played"])
    return rows


def get_matching_ids(q=None, genre=None, date_from=None, date_to=None,
                     after=None, before=None, cap=500) -> list[int]:
    """Ids for 'select all matching' -- one representative play per track."""
    where, params = _filters(q, genre, date_from, date_to, after, before)
    try:
        with _cursor() as (_c, cur):
            cur.execute(
                f"SELECT MAX(id) FROM recognized_songs {where} "
                f"GROUP BY title, artist ORDER BY MAX(recognized_at) DESC LIMIT %s",
                tuple(params) + (cap,),
            )
            return [r[0] for r in cur.fetchall()]
    except mysql.connector.Error as e:
        log.error("get_matching_ids error: %s", e)
        return []


def get_history(offset=0, limit=50, q=None, genre=None, date_from=None, date_to=None,
                after=None, before=None) -> list[dict]:
    """Ungrouped, chronological. Answers 'what was on Saturday at 9pm'."""
    where, params = _filters(q, genre, date_from, date_to, after, before)
    try:
        with _cursor(dictionary=True) as (_c, cur):
            cur.execute(
                f"SELECT id, title, artist, album, genre, duration, cover_url, recognized_at "
                f"FROM recognized_songs {where} "
                f"ORDER BY recognized_at DESC LIMIT %s OFFSET %s",
                tuple(params) + (limit, offset),
            )
            rows = cur.fetchall()
    except mysql.connector.Error as e:
        log.error("get_history error: %s", e)
        return []

    for r in rows:
        r["played_at"] = utc_to_local_str(r.pop("recognized_at"))
    return rows


def get_genres() -> list[str]:
    try:
        with _cursor() as (_c, cur):
            cur.execute(
                "SELECT genre, COUNT(*) c FROM recognized_songs "
                "WHERE genre IS NOT NULL AND genre <> '' "
                "GROUP BY genre ORDER BY c DESC"
            )
            return [r[0] for r in cur.fetchall()]
    except mysql.connector.Error as e:
        log.error("get_genres error: %s", e)
        return []


def get_tracks_by_ids(ids) -> list[dict]:
    clean = []
    for i in ids:
        try:
            clean.append(int(i))
        except (TypeError, ValueError):
            continue
    if not clean:
        return []

    placeholders = ",".join(["%s"] * len(clean))
    try:
        with _cursor(dictionary=True) as (_c, cur):
            cur.execute(
                f"SELECT id, title, artist, album, duration, cover_url "
                f"FROM recognized_songs WHERE id IN ({placeholders})",
                tuple(clean),
            )
            rows = cur.fetchall()
        order = {tid: n for n, tid in enumerate(clean)}
        rows.sort(key=lambda r: order.get(r["id"], 0))
        return rows
    except mysql.connector.Error as e:
        log.error("get_tracks_by_ids error: %s", e)
        return []


def get_cover_url(play_id: int) -> str | None:
    try:
        with _cursor() as (_c, cur):
            cur.execute(
                "SELECT cover_url FROM recognized_songs WHERE id = %s", (int(play_id),)
            )
            row = cur.fetchone()
            return row[0] if row else None
    except (mysql.connector.Error, ValueError) as e:
        log.warning("get_cover_url failed: %s", e)
        return None


def get_stats() -> dict:
    """Everything the Stats tab needs, in one round trip per panel."""
    off = _local_offset_minutes()
    shifted = "recognized_at + INTERVAL %s MINUTE"
    out: dict = {}
    try:
        with _cursor(dictionary=True) as (_c, cur):
            # Average known duration, used to estimate listening minutes for the
            # plays whose duration Shazam never returned. This self-corrects as
            # enrichment backfills real durations over time.
            cur.execute("SELECT AVG(duration) a FROM recognized_songs WHERE duration > 0")
            avg_dur = int((cur.fetchone() or {}).get("a") or 210)
            mins = "ROUND(SUM(COALESCE(NULLIF(duration, 0), %s)) / 60)"

            cur.execute(
                "SELECT COUNT(*) plays, COUNT(DISTINCT title, artist) tracks, "
                "COUNT(DISTINCT artist) artists, COUNT(DISTINCT album) albums, "
                f"{mins} AS minutes, "
                "MIN(recognized_at) first_play, MAX(recognized_at) last_play "
                "FROM recognized_songs", (avg_dur,)
            )
            t = cur.fetchone() or {}
            t["minutes"] = int(t.get("minutes") or 0)
            t["first_play"] = utc_to_local_str(t.get("first_play"))
            t["last_play"] = utc_to_local_str(t.get("last_play"))
            out["totals"] = t

            cur.execute(
                "SELECT artist AS label, COUNT(*) AS plays FROM recognized_songs "
                "GROUP BY artist ORDER BY plays DESC LIMIT 12"
            )
            out["top_artists"] = cur.fetchall()

            cur.execute(
                "SELECT title AS label, artist AS sub, COUNT(*) AS plays FROM recognized_songs "
                "GROUP BY title, artist ORDER BY plays DESC LIMIT 12"
            )
            out["top_tracks"] = cur.fetchall()

            cur.execute(
                "SELECT album AS label, artist AS sub, COUNT(*) AS plays FROM recognized_songs "
                "WHERE album IS NOT NULL AND album <> '' "
                "GROUP BY album, artist ORDER BY plays DESC LIMIT 12"
            )
            out["top_albums"] = cur.fetchall()

            cur.execute(
                "SELECT genre AS label, COUNT(*) AS plays FROM recognized_songs "
                "WHERE genre IS NOT NULL AND genre <> '' "
                "GROUP BY genre ORDER BY plays DESC LIMIT 12"
            )
            out["genres"] = cur.fetchall()

            cur.execute(
                f"SELECT HOUR({shifted}) AS hour, COUNT(*) AS plays, "
                f"{mins} AS minutes "
                f"FROM recognized_songs GROUP BY hour", (off, avg_dur)
            )
            by_hour = {int(r["hour"]): (int(r["plays"]), int(r["minutes"] or 0))
                       for r in cur.fetchall()}
            out["by_hour"] = [
                {"hour": h, "plays": by_hour.get(h, (0, 0))[0],
                 "minutes": by_hour.get(h, (0, 0))[1]} for h in range(24)
            ]

            # Per-day history for the calendar heatmap (~53 weeks): plays and
            # estimated listening minutes. The UI colours each cell by minutes.
            cur.execute(
                f"SELECT DATE({shifted}) AS day, COUNT(*) AS plays, "
                f"{mins} AS minutes FROM recognized_songs "
                f"WHERE recognized_at >= UTC_TIMESTAMP() - INTERVAL 371 DAY "
                f"GROUP BY day ORDER BY day", (off, avg_dur)
            )
            out["calendar"] = [
                {"day": str(r["day"]), "plays": int(r["plays"]),
                 "minutes": int(r["minutes"] or 0)} for r in cur.fetchall()
            ]

            # 7-day segment match rate (matched / (matched+missed)).
            cur.execute(
                "SELECT COALESCE(SUM(matched), 0) m, COALESCE(SUM(missed), 0) x "
                "FROM segment_counts WHERE day >= (CURDATE() - INTERVAL 7 DAY)"
            )
            r = cur.fetchone() or {}
            m, x = int(r.get("m") or 0), int(r.get("x") or 0)
            out["recognition"] = {"matched": m, "missed": x,
                                  "rate": (m / (m + x)) if (m + x) else None}

            # Artists first heard in the last 7 days.
            cur.execute(
                "SELECT artist FROM recognized_songs GROUP BY artist "
                "HAVING MIN(recognized_at) >= UTC_TIMESTAMP() - INTERVAL 7 DAY "
                "ORDER BY MIN(recognized_at) DESC LIMIT 12"
            )
            out["new_artists"] = [row["artist"] for row in cur.fetchall()]

            # Listening sessions: runs of plays with < 20 min between them.
            # Needs MySQL 8 window functions; degrade quietly on older servers.
            try:
                cur.execute(
                    "SELECT COUNT(*) sessions, COALESCE(MAX(cnt), 0) longest FROM ("
                    "  SELECT grp, COUNT(*) cnt FROM ("
                    "    SELECT SUM(newsess) OVER (ORDER BY recognized_at) grp FROM ("
                    "      SELECT recognized_at, CASE WHEN "
                    "        LAG(recognized_at) OVER (ORDER BY recognized_at) IS NULL OR "
                    "        TIMESTAMPDIFF(MINUTE, LAG(recognized_at) OVER "
                    "          (ORDER BY recognized_at), recognized_at) > 20 "
                    "        THEN 1 ELSE 0 END newsess FROM recognized_songs) a) b "
                    "  GROUP BY grp) c"
                )
                sr = cur.fetchone() or {}
                out["sessions"] = {"count": int(sr.get("sessions") or 0),
                                   "longest": int(sr.get("longest") or 0)}
            except mysql.connector.Error as se:
                log.debug("sessions query skipped: %s", se)
                out["sessions"] = {"count": None, "longest": None}
    except mysql.connector.Error as e:
        log.error("get_stats error: %s", e)
        return {"error": str(e)}
    return out
