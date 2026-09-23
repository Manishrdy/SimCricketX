"""Community image uploads: strict validation + compression to small WebP,
stored on local disk.

Pipeline (``process_image``, pure CPU over bytes):
  1. sniff the real format with Pillow (extension / MIME are ignored) —
     JPEG, PNG and WebP only; animated files keep their first frame
  2. reject anything over MAX_PIXELS before decoding (decompression bombs)
  3. JPEGs decode at reduced DCT scale via ``draft`` — keeps peak RAM low on
     the 1 GB production box
  4. apply EXIF orientation, then re-encode with NO metadata (GPS, device
     and timestamps are dropped)
  5. longest edge <= 1600 px; alpha kept only if actually used
  6. WebP quality ladder until <= TARGET_BYTES, shrinking to 1280 / 1024 px
     if needed; PNG input (screenshots) also tries lossless and keeps the
     smaller. Anything still over HARD_MAX_BYTES is rejected.
  7. 480 px thumbnail <= THUMB_TARGET_BYTES

Processing runs through ``utils.cpu_offload.run_offloaded`` so a large photo
never blocks the gevent hub (and with it every live match's Socket.IO
heartbeat), with at most two in flight to bound memory.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import threading
import uuid
from datetime import datetime, timedelta

from PIL import Image, ImageOps

from database import db
from database.models import CommunityImage
from utils.cpu_offload import run_offloaded

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 30_000_000
MAX_EDGE = 1600
FALLBACK_EDGES = (1280, 1024)
TARGET_BYTES = 200 * 1024
HARD_MAX_BYTES = 300 * 1024
QUALITY_LADDER = (80, 72, 64, 56, 50)
THUMB_EDGE = 480
THUMB_TARGET_BYTES = 40 * 1024
THUMB_LADDER = (60, 50, 40)
DAILY_UPLOAD_LIMIT = 20
DEFAULT_MIN_FREE_BYTES = 2 * 1024 ** 3
ACCEPTED_FORMATS = {"JPEG", "PNG", "WEBP"}

# Pillow's own guard as a second line (it raises at 2x this).
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

_processing_slots = threading.BoundedSemaphore(2)


class ImageRejected(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ── Pure processing ───────────────────────────────────────────────────────────

def _open_checked(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError:
        raise ImageRejected("That image is too large (too many pixels).")
    except Exception:
        raise ImageRejected("That file isn't a supported image. Use JPEG, PNG or WebP.")
    if img.format not in ACCEPTED_FORMATS:
        raise ImageRejected("Only JPEG, PNG and WebP images are allowed.")
    w, h = img.size
    if w * h > MAX_PIXELS:
        raise ImageRejected("That image is too large (over 30 megapixels).")
    if w < 16 or h < 16:
        raise ImageRejected("That image is too small.")
    return img


def _has_real_alpha(img: Image.Image) -> bool:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        alpha = img.convert("RGBA").getchannel("A")
        return alpha.getextrema()[0] < 255
    return False


def _normalise(img: Image.Image) -> Image.Image:
    fmt = img.format
    if fmt == "JPEG":
        img.draft("RGB", (MAX_EDGE * 2, MAX_EDGE * 2))
    if getattr(img, "is_animated", False):
        img.seek(0)
    try:
        img.load()
    except Exception:
        raise ImageRejected("That image is damaged or incomplete.")
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGBA") if _has_real_alpha(img) else img.convert("RGB")
    img.info.clear()
    return img


def _webp(img: Image.Image, **opts) -> bytes:
    buf = io.BytesIO()
    # exif/icc omitted on purpose: nothing identifying survives re-encode.
    img.save(buf, "WEBP", method=4, **opts)
    return buf.getvalue()


def _fit(img: Image.Image, edge: int) -> Image.Image:
    out = img.copy()
    out.thumbnail((edge, edge), Image.LANCZOS)
    return out


def _encode_full(img: Image.Image, try_lossless: bool) -> tuple[bytes, int, int]:
    best = None
    for edge in (MAX_EDGE,) + FALLBACK_EDGES:
        sized = _fit(img, edge)
        lossy = None
        for q in QUALITY_LADDER:
            lossy = _webp(sized, quality=q)
            if len(lossy) <= TARGET_BYTES:
                break
        options = [lossy]
        if try_lossless:
            options.append(_webp(sized, lossless=True, quality=80))
        data = min(options, key=len)
        if best is None or len(data) < len(best[0]):
            best = (data, sized.width, sized.height)
        if len(data) <= TARGET_BYTES:
            return best
    if len(best[0]) > HARD_MAX_BYTES:
        raise ImageRejected("That image can't be compressed small enough. Try a smaller screenshot.")
    return best


def _encode_thumb(img: Image.Image) -> bytes:
    sized = _fit(img, THUMB_EDGE)
    data = b""
    for q in THUMB_LADDER:
        data = _webp(sized, quality=q)
        if len(data) <= THUMB_TARGET_BYTES:
            break
    return data


def process_image(data: bytes) -> dict:
    """Validate and compress raw upload bytes. Pure: safe on a worker thread."""
    if not data:
        raise ImageRejected("Empty file.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageRejected("Images must be 10 MB or smaller.", status=413)
    img = _open_checked(data)
    source_format = img.format
    img = _normalise(img)
    full, width, height = _encode_full(img, try_lossless=source_format == "PNG")
    thumb = _encode_thumb(img)
    return {"full": full, "thumb": thumb, "width": width, "height": height,
            "sha256": hashlib.sha256(full).hexdigest()}


def _process_quietly(data: bytes) -> dict:
    """process_image for the thread pool. gevent prints a traceback for any
    exception a pool task raises, so an ordinary rejected upload would spam the
    log; hand the rejection back as data and re-raise it on the request side."""
    try:
        return process_image(data)
    except ImageRejected as exc:
        return {"rejected": (exc.message, exc.status)}


# ── Storage ───────────────────────────────────────────────────────────────────

def media_root(app=None) -> str:
    from flask import current_app
    app = app or current_app
    root = app.config.get("COMMUNITY_MEDIA_DIR") or os.environ.get("COMMUNITY_MEDIA_DIR") or \
        os.path.join(app.root_path, "data", "community_media")
    os.makedirs(root, exist_ok=True)
    return root


def _abs(rel: str) -> str:
    root = os.path.realpath(media_root())
    path = os.path.realpath(os.path.join(root, rel))
    if not path.startswith(root + os.sep):
        raise ValueError("path escapes media root")
    return path


def _write_atomic(rel: str, data: bytes) -> None:
    path = _abs(rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def check_upload_allowed(user) -> None:
    from flask import current_app
    min_free = current_app.config.get("COMMUNITY_MIN_FREE_BYTES", DEFAULT_MIN_FREE_BYTES)
    if shutil.disk_usage(media_root()).free < min_free:
        raise ImageRejected("Image uploads are temporarily unavailable.", status=503)
    if not getattr(user, "is_admin", False):
        since = datetime.utcnow() - timedelta(days=1)
        count = CommunityImage.query.filter(CommunityImage.uploader_id == user.id,
                                            CommunityImage.created_at >= since).count()
        if count >= DAILY_UPLOAD_LIMIT:
            raise ImageRejected(f"You can upload {DAILY_UPLOAD_LIMIT} images per day.", status=429)


def store_upload(user, data: bytes) -> CommunityImage:
    """Validate, compress, write and record one upload (unattached)."""
    check_upload_allowed(user)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageRejected("Images must be 10 MB or smaller.", status=413)
    with _processing_slots:
        result = run_offloaded(_process_quietly, data)
    if "rejected" in result:
        raise ImageRejected(*result["rejected"])

    dupe = CommunityImage.query.filter_by(sha256=result["sha256"], purged_at=None).first()
    if dupe is not None and os.path.exists(_abs(dupe.path)):
        path, thumb_path = dupe.path, dupe.thumb_path
    else:
        name = uuid.uuid4().hex
        shard = f"{name[:2]}/{name[2:4]}"
        path, thumb_path = f"{shard}/{name}.webp", f"{shard}/{name}_t.webp"
        _write_atomic(path, result["full"])
        _write_atomic(thumb_path, result["thumb"])

    img = CommunityImage(public_id=uuid.uuid4().hex, uploader_id=user.id, path=path, thumb_path=thumb_path,
                         bytes=len(result["full"]), thumb_bytes=len(result["thumb"]),
                         width=result["width"], height=result["height"], sha256=result["sha256"])
    db.session.add(img)
    db.session.commit()
    return img


def delete_image_files(img: CommunityImage) -> None:
    """Unlink the files unless another live row shares them (sha256 dedupe),
    and mark the row purged. Caller commits."""
    shared = (CommunityImage.query.filter(CommunityImage.path == img.path, CommunityImage.id != img.id,
                                          CommunityImage.purged_at.is_(None)).count())
    if not shared:
        for rel in (img.path, img.thumb_path):
            try:
                os.remove(_abs(rel))
            except FileNotFoundError:
                pass
    img.purged_at = datetime.utcnow()


def file_path_for(img: CommunityImage, variant: str) -> str:
    return _abs(img.thumb_path if variant == "thumb" else img.path)
