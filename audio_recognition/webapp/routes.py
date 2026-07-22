import logging
import re

import requests
from flask import (
    Blueprint, Response, abort, current_app, jsonify, redirect, render_template,
    request, send_file, session, stream_with_context, url_for,
)

from .. import corrections, covers, state
from .. import config
from .. import library
from .. import autoplaylist
from . import auth
from . import envedit
from ..services import spotify
from ..services import tidal
from ..services import local_library

from ..plex import client as plex
from ..storage import db as store

log = logging.getLogger("audio_recognition.webapp")

bp = Blueprint("routes", __name__)

_TAG_RE = re.compile(r"<[^>]+>")
_READMORE_RE = re.compile(r"read more.*", re.IGNORECASE | re.DOTALL)


def _int_arg(name, default, lo, hi):
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        return default


def _str_arg(name, maxlen=120):
    v = (request.args.get(name) or "").strip()
    return v[:maxlen] or None


def _common_filters():
    return {
        "q": _str_arg("q"),
        "genre": _str_arg("genre"),
        "date_from": _str_arg("from", 10),
        "date_to": _str_arg("to", 10),
        "after": _str_arg("after", 25),
        "before": _str_arg("before", 25),
    }


# --- now playing ---------------------------------------------------------

@bp.route("/api/now_playing")
def now_playing():
    return jsonify(state.snapshot())


# --- tracks / history ----------------------------------------------------

@bp.route("/archive_data")
def archive_data():
    return jsonify(store.get_archive(
        offset=_int_arg("offset", 0, 0, 1_000_000),
        limit=_int_arg("limit", 25, 1, config.ARCHIVE_MAX_LIMIT),
        sort=_str_arg("sort") or "recent",
        merge_variants=request.args.get("merge") == "1",
        **_common_filters(),
    ))


@bp.route("/api/history")
def history():
    return jsonify(store.get_history(
        offset=_int_arg("offset", 0, 0, 1_000_000),
        limit=_int_arg("limit", 50, 1, config.ARCHIVE_MAX_LIMIT),
        **_common_filters(),
    ))


@bp.route("/api/genres")
def genres():
    return jsonify(store.get_genres())


@bp.route("/api/stats")
def stats():
    return jsonify(store.get_stats())


@bp.route("/api/matching_ids")
def matching_ids():
    return jsonify(store.get_matching_ids(**_common_filters()))


@bp.route("/api/in_library", methods=["POST"])
def in_library():
    """Which of these are actually in Plex? The playlist silently skips the rest."""
    payload = request.get_json(silent=True) or {}
    tracks = payload.get("tracks") or []
    if not library.configured():
        return jsonify({"configured": False, "found": {}})
    rows = [t for t in tracks[:60] if t.get("id") is not None]
    present = library.presence_batch([(t.get("artist", ""), t.get("title", "")) for t in rows])
    found = {str(t["id"]): present.get((t.get("artist", ""), t.get("title", "")), False)
             for t in rows}
    return jsonify({"configured": True, "found": found})


# --- edits ---------------------------------------------------------------

@bp.route("/api/play/<int:play_id>", methods=["DELETE"])
def delete_play(play_id):
    return jsonify({"deleted": store.delete_play(play_id)})


@bp.route("/api/forget", methods=["POST"])
def forget():
    payload = request.get_json(silent=True) or {}
    title, artist = payload.get("title"), payload.get("artist")
    if not title or not artist:
        return jsonify({"error": "title and artist required"}), 400
    return jsonify({"deleted": store.forget_track(title, artist)})


# --- playlist ------------------------------------------------------------

@bp.route("/download_playlist", methods=["POST"])
def download_playlist():
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "Select at least one track."}), 400
    if not library.configured():
        return jsonify({"error": "No music library configured (set up Plex or a "
                        "local folder in Config)."}), 400

    tracks = store.get_tracks_by_ids(ids)
    if not tracks:
        return jsonify({"error": "No matching tracks."}), 404

    lines, missing = ["#EXTM3U"], []
    for t in tracks:
        r = library.resolve(t["artist"], t["title"])
        if not r:
            missing.append(f"{t['artist']} - {t['title']}")
            continue
        duration = t.get("duration") or r.get("duration") or -1
        lines.append(f"#EXTINF:{int(duration)},{t['artist']} - {t['title']}")
        if r["backend"] == "local":
            lines.append(r["location"])                       # a real file path
        elif config.PLAYLIST_EMBED_TOKEN:
            lines.append(f"{config.PLEX_BASE_URL}{r['part_key']}?X-Plex-Token={config.PLEX_TOKEN}")
        else:
            lines.append(url_for("routes.stream", track_id=t["id"], _external=True))

    if len(lines) == 1:
        return jsonify({"error": f"None of these were found in {library.name()}."}), 404
    if missing:
        log.info("Not in library, skipped: %s", "; ".join(missing))

    return Response(
        "\n".join(lines) + "\n",
        mimetype="audio/x-mpegurl",
        headers={
            "Content-Disposition": 'attachment; filename="playlist.m3u"',
            "X-Skipped-Count": str(len(missing)),
        },
    )


@bp.route("/export_list", methods=["POST"])
def export_list():
    """A plain 'Artist - Title' text list of the selected tracks -- needs no
    library at all, for pasting into a transfer service or keeping a record."""
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids") or []
    if not ids:
        return jsonify({"error": "Select at least one track."}), 400
    tracks = store.get_tracks_by_ids(ids)
    body = "\n".join(f"{t['artist']} - {t['title']}" for t in tracks) + "\n"
    return Response(body, mimetype="text/plain",
                    headers={"Content-Disposition": 'attachment; filename="tracks.txt"'})


@bp.route("/stream/<int:track_id>")
def stream(track_id):
    """Proxy the Plex part so the token never leaves the server."""
    rows = store.get_tracks_by_ids([track_id])
    if not rows:
        abort(404)
    try:
        match = plex.find_track(rows[0]["artist"], rows[0]["title"])
    except Exception:
        match = None   # Plex unreachable -> behave as "no match" for this request
    if not match:
        abort(404)

    try:
        upstream = plex.open_stream(match["part_key"], request.headers.get("Range"))
    except requests.RequestException as e:
        log.warning("Plex stream failed: %s", e)
        abort(502)
    if upstream.status_code >= 400:
        upstream.close()
        abort(upstream.status_code)

    keep = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")
    headers = {k: v for k, v in upstream.headers.items() if k in keep}

    def generate():
        try:
            for chunk in upstream.iter_content(64 * 1024):
                yield chunk
        finally:
            upstream.close()

    return Response(stream_with_context(generate()),
                    status=upstream.status_code, headers=headers)


# --- cover art -----------------------------------------------------------

def _serve_cover(url):
    """Serve from the local cache, fetching once on a miss. After the first
    hit this never touches Shazam's CDN again -- which is the point, because
    those URLs expire and the archive would otherwise go blank over time."""
    if not url:
        abort(404)
    path = covers.ensure(url)
    if not path:
        abort(404)
    resp = send_file(path, mimetype="image/jpeg", conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@bp.route("/cover/<int:play_id>.jpg")
@bp.route("/cover/<int:play_id>")
def cover(play_id):
    return _serve_cover(store.get_cover_url(play_id))


@bp.route("/cover/now")
def cover_now():
    return _serve_cover(state.snapshot().get("cover_url"))


# --- trivia --------------------------------------------------------------

@bp.route("/album_trivia")
def album_trivia():
    album = (request.args.get("album") or "").strip()
    artist = (request.args.get("artist") or "").strip()
    if not album or not artist or not config.LASTFM_API_KEY:
        return jsonify({"trivia": ""})

    try:
        resp = requests.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={"method": "album.getInfo", "artist": artist, "album": album,
                    "api_key": config.LASTFM_API_KEY, "format": "json", "autocorrect": 1},
            timeout=config.LASTFM_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("Last.fm lookup failed: %s", e)
        return jsonify({"trivia": ""})

    summary = ((data.get("album") or {}).get("wiki") or {}).get("summary", "")
    summary = _READMORE_RE.sub("", _TAG_RE.sub("", summary)).strip()
    return jsonify({"trivia": summary})


@bp.route("/healthz")
def healthz():
    age = state.signal_age()
    rate = store.get_recognition_rate(7)
    return jsonify({
        "ok": True,
        "plex": plex.configured(),
        "signal_age_sec": int(age) if age is not None else None,
        "recognition_rate": rate["rate"],
    })


# --- want-list (heard, but not in Plex) ----------------------------------

@bp.route("/api/wantlist")
def wantlist():
    """Distinct recognized tracks NOT in your library (Plex or a local folder) --
    an acquisition list. Checks run concurrently and are cached."""
    if not library.configured():
        return jsonify({"configured": False, "tracks": []})
    if request.args.get("refresh"):
        try:
            plex.clear_match_cache()
        except Exception:
            pass
    rows = store.get_distinct_tracks(cap=600)
    present = library.presence_batch([(r["artist"], r["title"]) for r in rows])
    # Only CONFIRMED misses belong here. A None means the library couldn't be
    # checked (Plex unreachable) -- listing those as "not in Plex" is a lie.
    out = [r for r in rows
           if present.get((r["artist"], r["title"]), None) is False]
    unknown = sum(1 for r in rows
                  if present.get((r["artist"], r["title"]), None) is None)
    return jsonify({"configured": True, "tracks": out[:200], "unknown": unknown})


# --- correction ----------------------------------------------------------

@bp.route("/api/fix", methods=["POST"])
def fix():
    """Relabel a mis-recognized track and remember the correction so the same
    raw recognition is auto-fixed in the future."""
    p = request.get_json(silent=True) or {}
    old_a, old_t = p.get("old_artist"), p.get("old_title")
    new_a = (p.get("artist") or "").strip()
    new_t = (p.get("title") or "").strip()
    if not (old_a and old_t and new_a and new_t):
        return jsonify({"error": "old_title/old_artist and new title/artist required"}), 400
    relabeled = corrections.add(old_a, old_t, new_a, new_t)
    return jsonify({"relabeled": relabeled, "artist": new_a, "title": new_t})


# --- create a real Plex playlist -----------------------------------------

@bp.route("/create_plex_playlist", methods=["POST"])
def create_plex_playlist():
    p = request.get_json(silent=True) or {}
    ids = p.get("ids") or []
    name = (p.get("name") or "").strip() or "Heard on the stereo"
    if not plex.configured():
        return jsonify({"error": "Plex is not configured."}), 400

    keys, missing = [], []
    for t in store.get_tracks_by_ids(ids):
        try:
            rk = plex.match_rating_key(t["artist"], t["title"], t.get("album"))
        except Exception:
            rk = None
        if rk:
            keys.append(rk)
        else:
            missing.append(f"{t['artist']} - {t['title']}")
    if not keys:
        return jsonify({"error": "None of these are in your Plex library."}), 404

    try:
        result = plex.create_or_append_playlist(name, keys)
    except Exception as e:
        log.warning("Plex playlist failed: %s", e)
        return jsonify({"error": "Plex rejected the playlist."}), 502
    return jsonify({
        "playlist": name,
        "created": result.get("created"),
        "added": result.get("added", len(keys)),          # newly added to the playlist
        "already_in_playlist": result.get("skipped", 0),  # were already there
        "not_in_library": len(missing),                   # couldn't be matched in Plex
        "playlist_key": result.get("playlist_key"),
    })


# --- spotify -------------------------------------------------------------

@bp.route("/spotify/login")
def spotify_login():
    url = spotify.login_url()
    if not url:
        return jsonify({"error": "Spotify is not configured."}), 400
    return redirect(url)


@bp.route("/spotify/callback")
def spotify_callback():
    err = request.args.get("error")
    if err:
        return redirect("/config?spotify=denied")
    ok = spotify.handle_callback(request.args.get("code", ""))
    return redirect("/config?spotify=" + ("connected" if ok else "failed"))


@bp.route("/create_spotify_playlist", methods=["POST"])
def create_spotify_playlist():
    p = request.get_json(silent=True) or {}
    ids = p.get("ids") or []
    name = (p.get("name") or "").strip() or "Heard on the stereo"
    if not spotify.configured():
        return jsonify({"error": "Spotify is not configured (see Config)."}), 400
    if not spotify.connected():
        return jsonify({"error": "Connect Spotify on the Config page first."}), 400
    if not ids:
        return jsonify({"error": "Select at least one track."}), 400

    tracks = [{"artist": t["artist"], "title": t["title"], "album": t.get("album")}
              for t in store.get_tracks_by_ids(ids)]
    try:
        result = spotify.create_playlist(name, tracks)
    except Exception as e:
        log.warning("Spotify playlist failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify({"playlist": name, **result})


# --- tidal ---------------------------------------------------------------

@bp.route("/tidal/login", methods=["POST"])
def tidal_login():
    if not tidal.configured():
        return jsonify({"error": "Tidal is not enabled (see Config)."}), 400
    info = tidal.begin_login()
    if not info:
        return jsonify({"error": "Could not start Tidal login."}), 502
    return jsonify(info)


@bp.route("/tidal/login/status")
def tidal_login_status():
    return jsonify(tidal.login_status())


@bp.route("/create_tidal_playlist", methods=["POST"])
def create_tidal_playlist():
    p = request.get_json(silent=True) or {}
    ids = p.get("ids") or []
    name = (p.get("name") or "").strip() or "Heard on the stereo"
    if not tidal.configured():
        return jsonify({"error": "Tidal is not enabled (see Config)."}), 400
    if not tidal.connected():
        return jsonify({"error": "Authorize Tidal first: run "
                        "`python -m audio_recognition.services.tidal login` on the host."}), 400
    if not ids:
        return jsonify({"error": "Select at least one track."}), 400

    tracks = [{"artist": t["artist"], "title": t["title"], "album": t.get("album")}
              for t in store.get_tracks_by_ids(ids)]
    try:
        result = tidal.create_playlist(name, tracks)
    except Exception as e:
        log.warning("Tidal playlist failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify({"playlist": name, **result})


# --- config / status page ------------------------------------------------

@bp.route("/config")
def config_page():
    status = {
        "plex": {"configured": plex.configured()},
        "local": {"configured": local_library.configured()},
        "library": {"backend": library.name()},
        "spotify": {"configured": spotify.configured(),
                    "connected": spotify.connected() if spotify.configured() else False},
        "tidal": {"configured": tidal.configured(),
                  "connected": tidal.connected() if tidal.configured() else False},
        "lastfm": {"configured": bool(config.LASTFM_API_KEY)},
        "capture": {"device": config.ALSA_DEVICE,
                    "channels": int(config.CAPTURE_CHANNELS),
                    "rate": int(config.SAMPLE_RATE)},
        "display": {"enabled": bool(config.DISPLAY_ENABLED),
                    "size": f"{config.DISPLAY_SIZE[0]}x{config.DISPLAY_SIZE[1]}"},
        "autoplaylist": {"targets": autoplaylist.targets(),
                         "name": config.AUTO_PLAYLIST_NAME,
                         "enabled": autoplaylist.enabled(),
                         "queued": store.autoplaylist_queue_depth()},
    }
    return render_template(
        "config.html", status=status, login_enabled=auth.login_enabled(),
        editable=envedit.available(),
        auth_on=auth.enabled(),
        env_file=config.CONFIG_PATH,
        sections=envedit.fields_for_view() if envedit.available() else {},
    )


@bp.route("/api/releases")
def releases():
    """Candidate releases (albums) for a track. Uses Last.fm when a key is set
    (it's what you already have configured), falling back to MusicBrainz."""
    artist = request.args.get("artist", "").strip()
    title = request.args.get("title", "").strip()
    if not artist or not title:
        return jsonify({"releases": []})
    from .. import lastfm, musicbrainz
    rels, source = [], None
    if lastfm.configured():
        rels = lastfm.search_releases(artist, title)
        source = "last.fm"
    if not rels:
        rels = musicbrainz.search_releases(artist, title)
        source = "musicbrainz"
    log.info("Release lookup for %s - %s: %d result(s) via %s",
             artist, title, len(rels), source)
    return jsonify({"releases": rels, "source": source})


@bp.route("/api/set_release", methods=["POST"])
def set_release():
    p = request.get_json(silent=True) or {}
    artist = (p.get("artist") or "").strip()
    title = (p.get("title") or "").strip()
    album = (p.get("album") or "").strip()
    cover_url = (p.get("cover_url") or "").strip() or None
    if not (artist and title and album):
        return jsonify({"error": "artist, title, and album are required."}), 400
    n = store.set_album_override(artist, title, album, cover_url)
    return jsonify({"album": album, "relabeled": n})


@bp.route("/api/autoplaylist/backfill", methods=["POST"])
def autoplaylist_backfill():
    if not autoplaylist.enabled():
        return jsonify({"error": "No auto-playlist services are enabled."}), 400
    queued = autoplaylist.backfill()
    return jsonify({"queued": queued})


@bp.route("/api/plex/search")
def plex_search():
    """Candidate Plex tracks for the manual want-list assignment picker."""
    q = request.args.get("q", "").strip()
    try:
        return jsonify({"tracks": plex.search_candidates(q)})
    except Exception as e:
        return jsonify({"tracks": [], "error": f"Plex search failed: {e}"}), 502


@bp.route("/api/plex/browse")
def plex_browse():
    """Drill-down for the manual picker: artists -> albums -> tracks."""
    try:
        if request.args.get("album"):
            return jsonify({"tracks": plex.browse_tracks(request.args["album"])})
        if request.args.get("artist_key"):
            return jsonify({"albums": plex.browse_albums(request.args["artist_key"])})
        return jsonify({"artists": plex.browse_artists(request.args.get("q", ""))})
    except Exception as e:
        return jsonify({"error": f"Plex browse failed: {e}"}), 502


@bp.route("/api/plex/link", methods=["POST"])
def plex_link():
    """Assign a recognized track to a specific Plex item (or clear it)."""
    p = request.get_json(silent=True) or {}
    artist = (p.get("artist") or "").strip()
    title = (p.get("title") or "").strip()
    if not (artist and title):
        return jsonify({"error": "artist and title are required"}), 400
    key = (p.get("rating_key") or "").strip()
    if not key:
        store.clear_library_link(artist, title)
    else:
        store.set_library_link(artist, title, key, p.get("label"))
    try:
        plex.clear_match_cache()
    except Exception:
        pass
    return jsonify({"ok": True, "linked": bool(key)})


@bp.route("/api/display_test", methods=["POST"])
def display_test():
    """Draw a test card so you can confirm the screen works without waiting for
    the next track. Reports what actually happened."""
    from ..display import image_ops
    if not config.DISPLAY_ENABLED:
        return jsonify({"error": "The display is turned off."}), 400
    try:
        image_ops.display_text()   # same card the app shows while identifying
    except Exception as e:
        return jsonify({"error": f"Display failed: {e}"}), 500
    proc = getattr(image_ops, "_feh_process", None)
    if proc is not None and proc.poll() is None:
        return jsonify({"ok": True, "detail": "Sent 'Identifying Track' to the display."})
    return jsonify({"ok": False,
                    "detail": "No viewer is running — see musicguru.log for why "
                              "(feh needs X; install fbi for framebuffer)."})


@bp.route("/api/capture_devices")
def capture_devices():
    """List ALSA capture devices (arecord -l) so the user can find their line-in
    or mic and set AR_ALSA_DEVICE (e.g. hw:1,0)."""
    import subprocess
    try:
        out = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5)
        text = (out.stdout or "") + (out.stderr or "")
    except FileNotFoundError:
        text = "arecord not found (install alsa-utils)."
    except Exception as e:
        text = f"Could not list devices: {e}"
    return jsonify({"text": text.strip() or "No capture devices found."})


@bp.route("/config/save", methods=["POST"])
def config_save():
    if not envedit.available():
        abort(404)
    ok, msg = envedit.apply_form(request.form)
    if ok:
        try:
            changed = config.reload()
            # Re-apply every subsystem that caches settings, so nothing needs a
            # restart: service clients, logging handlers, the DB pool, and the
            # album-art display.
            plex.reset(); spotify.reset(); tidal.reset()
            from .. import logging_setup
            from ..storage import db as _db
            from ..display import image_ops as _display
            logging_setup.reconfigure()
            _db.reset_pool()
            _display.apply_display_setting()
            msg = "Saved and applied — no restart needed."
        except Exception as e:
            log.warning("Config reload failed: %s", e)
            msg = "Saved, but live reload failed — restart to apply."
    from urllib.parse import quote
    url = f"/config?saved={'1' if ok else '0'}&msg={quote(msg)}"
    tok = request.form.get("token") or request.args.get("token")
    if tok:
        url += f"&token={quote(tok)}"
    return redirect(url)


# --- prometheus metrics --------------------------------------------------

@bp.route("/metrics")
def metrics():
    m = store.get_metrics()
    rate = store.get_recognition_rate(7)
    age = state.signal_age()
    snap = state.snapshot()
    lines = [
        "# HELP ar_plays_total Recognized plays logged",
        "# TYPE ar_plays_total counter",
        f"ar_plays_total {int(m.get('plays', 0))}",
        "# HELP ar_tracks_total Distinct tracks",
        "# TYPE ar_tracks_total gauge",
        f"ar_tracks_total {int(m.get('tracks', 0))}",
        "# HELP ar_recognition_rate 7-day segment match rate",
        "# TYPE ar_recognition_rate gauge",
        f"ar_recognition_rate {rate['rate'] if rate['rate'] is not None else 0}",
        "# HELP ar_signal_age_seconds Seconds since audio was last heard (-1 = never)",
        "# TYPE ar_signal_age_seconds gauge",
        f"ar_signal_age_seconds {int(age) if age is not None else -1}",
        "# HELP ar_now_playing 1 if a track is currently identified",
        "# TYPE ar_now_playing gauge",
        f"ar_now_playing {1 if snap.get('playing') else 0}",
        "# HELP ar_up 1 if the web app is serving",
        "# TYPE ar_up gauge",
        "ar_up 1",
    ]
    return Response("\n".join(lines) + "\n", mimetype="text/plain; version=0.0.4")


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """First-run: create the mandatory login. Disabled once credentials exist."""
    if auth.login_enabled():
        return redirect(url_for("routes.index_page"))
    error = None
    if request.method == "POST":
        user = request.form.get("username", "").strip()
        pw = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if not user or not pw:
            error = "Username and password are required."
        elif len(pw) < 8:
            error = "Use a password of at least 8 characters."
        elif pw != confirm:
            error = "The passwords don't match."
        else:
            ok, msg = envedit.set_credentials(user, pw)
            if not ok:
                error = msg
            else:
                current_app.secret_key = auth.secret_key()  # stable across restarts
                session.permanent = True
                session["auth"] = True
                session["user"] = user
                return redirect(url_for("routes.index_page"))
    return render_template("setup.html", error=error), (400 if error else 200)


@bp.route("/login", methods=["GET", "POST"])
def auth_login():
    if not auth.login_enabled():
        return redirect(url_for("routes.index_page"))
    nxt = request.args.get("next") or request.form.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"   # only local redirects
    if session.get("auth"):
        return redirect(nxt)
    error = None
    if request.method == "POST":
        if auth.check_login(request.form.get("username", ""),
                            request.form.get("password", "")):
            session.permanent = True
            session["auth"] = True
            session["user"] = request.form.get("username", "")
            return redirect(nxt)
        error = "Incorrect username or password."
    return render_template("login.html", error=error, next=nxt), (401 if error else 200)


@bp.route("/logout")
def auth_logout():
    session.clear()
    return redirect(url_for("routes.auth_login"))


@bp.route("/")
def index_page():
    from .. import __version__
    return render_template("index.html", login_enabled=auth.login_enabled(),
                           version=__version__, plex_on=plex.configured())


@bp.route("/docs")
def docs_page():
    return render_template("docs.html")
