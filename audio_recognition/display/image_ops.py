import atexit
import hashlib
import logging
import os
import subprocess
import time
import tempfile
import time
from io import BytesIO

import requests
from PIL import Image, ImageDraw, ImageFont

from .. import config

log = logging.getLogger("audio_recognition.display")

_feh_process: subprocess.Popen | None = None
_last_hash: str | None = None
_font: ImageFont.ImageFont | None = None


def _get_font():
    """The old call was ImageFont.truetype('DejaVuSans-Bold.ttf', 40) with a bare
    filename. PIL almost never finds that, so it silently fell back to
    load_default() -- a ~10px bitmap face, illegible on an 800x480 panel."""
    global _font
    if _font is None:
        try:
            _font = ImageFont.truetype(config.FONT_PATH, config.FONT_SIZE)
        except OSError:
            log.warning("Font not found at %s; falling back to PIL default", config.FONT_PATH)
            try:
                _font = ImageFont.load_default(size=config.FONT_SIZE)  # Pillow >= 10.1
            except TypeError:
                _font = ImageFont.load_default()
    return _font


def download_image_with_retries(url, retries=config.IMAGE_RETRIES, timeout=config.IMAGE_TIMEOUT):
    """Blocking. Call via asyncio.to_thread from the pipeline."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
            if resp.status_code == 200:
                ctype = resp.headers.get("Content-Type", "")
                if ctype and not ctype.startswith("image/"):
                    log.warning("Cover URL returned %s, not an image", ctype)
                    return None
                buf = BytesIO()
                for chunk in resp.iter_content(64 * 1024):
                    buf.write(chunk)
                    if buf.tell() > config.IMAGE_MAX_BYTES:
                        log.warning("Cover art exceeds %d bytes; aborting", config.IMAGE_MAX_BYTES)
                        return None
                return buf.getvalue()
            log.warning("Attempt %d: image download failed with status %s", attempt, resp.status_code)
        except requests.RequestException as e:
            log.warning("Attempt %d: image download exception: %s", attempt, e)
        if attempt < retries:  # no pointless sleep after the final attempt
            time.sleep(1)
    return None


def display_text(text: str = "Identifying Audio") -> None:
    if not config.DISPLAY_ENABLED:
        return
    img = Image.new("RGB", config.DISPLAY_SIZE, "black")
    draw = ImageDraw.Draw(img)
    font = _get_font()
    # Subtract the bbox origin; the old code ignored it and drew slightly off-center.
    x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font)
    w, h = x1 - x0, y1 - y0
    pos = ((config.DISPLAY_SIZE[0] - w) // 2 - x0, (config.DISPLAY_SIZE[1] - h) // 2 - y0)
    draw.text(pos, text, fill="white", font=font)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=90)
    resize_and_display(buf.getvalue())


def _atomic_save(canvas: Image.Image, path: str) -> None:
    """feh --reload polls the file; a partial write would show a torn image."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".jpg")
    os.close(fd)
    try:
        canvas.save(tmp, "JPEG", quality=90)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _viewer_commands() -> list:
    """Viewer command-lines to try, in order. AR_DISPLAY_CMD overrides (use
    {file} for the image path). feh needs X; fbi/fbv draw straight to the
    framebuffer on a headless Pi, so they're tried as fallbacks."""
    custom = getattr(config, "DISPLAY_CMD", "") or ""
    if custom.strip():
        return [[p.replace("{file}", config.COVER_ART_FILE)
                 for p in custom.split()]]
    fb = getattr(config, "DISPLAY_FB", "/dev/fb0")
    return [
        ["feh", "--fullscreen", "--hide-pointer",
         "--reload", str(config.FEH_RELOAD_SEC), config.COVER_ART_FILE],
        ["fbi", "-d", fb, "-T", "1", "-noverbose", "-a", "-cachemem", "0",
         config.COVER_ART_FILE],
        ["fbv", "-d", fb, "-f", "-r", config.COVER_ART_FILE],
    ]


def _ensure_viewer() -> None:
    """Start an image viewer once and let it poll the file.

    Errors used to go to /dev/null, so a viewer that couldn't open a display
    failed completely silently. Now the failure reason is logged, and we fall
    back from feh (needs X) to framebuffer viewers.
    """
    global _feh_process
    if _feh_process is not None and _feh_process.poll() is None:
        return
    if not os.path.exists(config.COVER_ART_FILE):
        return   # nothing to show yet; caller writes the file first

    tried = []
    for cmd in _viewer_commands():
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
        except FileNotFoundError:
            tried.append(f"{cmd[0]}: not installed")
            continue
        except Exception as e:
            tried.append(f"{cmd[0]}: {e}")
            continue
        # Give it a moment: a viewer that can't open a display dies immediately.
        time.sleep(0.6)
        if proc.poll() is None:
            _feh_process = proc
            log.info("display: started %s (pid %s)", cmd[0], proc.pid)
            return
        err = ""
        try:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
        except Exception:
            pass
        tried.append(f"{cmd[0]}: exited rc={proc.returncode}"
                     + (f" -- {err.splitlines()[0][:160]}" if err else ""))

    _feh_process = None
    log.error("display: no viewer could start. Tried -> %s", "; ".join(tried))
    if not os.environ.get("DISPLAY"):
        log.error("display: DISPLAY is unset -- feh needs X. Install fbi "
                  "(apt install fbi) for framebuffer output, or set "
                  "AR_DISPLAY_CMD to your own viewer.")


def resize_and_display(img_data: bytes) -> None:
    global _last_hash
    if not config.DISPLAY_ENABLED or not img_data:
        return

    curr_hash = hashlib.md5(img_data).hexdigest()
    if curr_hash == _last_hash and os.path.exists(config.COVER_ART_FILE):
        return

    try:
        img = Image.open(BytesIO(img_data))
        img.load()
    except Exception as e:
        log.warning("Could not decode cover art: %s", e)
        return

    if img.mode != "RGB":
        img = img.convert("RGB")

    sw, sh = config.DISPLAY_SIZE
    iw, ih = img.size
    if iw == 0 or ih == 0:
        return
    ratio = iw / ih
    if sw / sh > ratio:
        new_w, new_h = max(1, int(sh * ratio)), sh
    else:
        new_w, new_h = sw, max(1, int(sw / ratio))

    canvas = Image.new("RGB", config.DISPLAY_SIZE, "black")
    canvas.paste(img.resize((new_w, new_h), Image.LANCZOS), ((sw - new_w) // 2, (sh - new_h) // 2))

    try:
        _atomic_save(canvas, config.COVER_ART_FILE)
    except OSError as e:
        log.warning("Could not write %s: %s", config.COVER_ART_FILE, e)
        return

    _last_hash = curr_hash
    _ensure_viewer()


def shutdown_display() -> None:
    """The old code never killed feh, so it survived every restart."""
    global _feh_process
    if _feh_process is None:
        return
    proc, _feh_process = _feh_process, None
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


atexit.register(shutdown_display)


def apply_display_setting() -> None:
    """Called after a config reload. Turning it OFF tears the viewer down. Turning
    it ON paints something immediately -- waiting for the next track change made
    a freshly enabled display look broken."""
    if not config.DISPLAY_ENABLED:
        try:
            shutdown_display()
        except Exception:
            pass
        return
    try:
        global _last_hash
        _last_hash = None          # force the next cover to redraw at the new size
        if os.path.exists(config.COVER_ART_FILE):
            _ensure_viewer()       # re-show the art already on disk
        else:
            display_text("musicguru")
    except Exception as e:
        log.warning("display: couldn't start on enable: %s", e)
