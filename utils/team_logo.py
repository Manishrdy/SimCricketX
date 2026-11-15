"""Team logo normalisation: the single place an uploaded logo becomes stored bytes.

Every path that accepts a logo (team create, team edit, the Manage Teams
upload endpoint) goes through ``normalize_logo`` so the rules cannot drift
between them.

Input is PNG only. Output is always our own re-encode: a 256x256 RGBA PNG
with the artwork fitted inside and centred on a transparent canvas (padded,
never cropped). Because only the re-encode is stored, metadata, text chunks,
extra APNG frames and anything appended after the image are dropped.
"""

import hashlib
import io

from PIL import Image

MAX_UPLOAD_BYTES = 1024 * 1024
MIN_EDGE = 64
MAX_EDGE = 2048
STORED_EDGE = 256
MAX_STORED_BYTES = 64 * 1024

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_PIXELS = MAX_EDGE * MAX_EDGE


class LogoError(ValueError):
    """The upload cannot be used as a logo. ``str(exc)`` is safe to show the user."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _encode(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def normalize_logo(data):
    """Return ``(png_bytes, sha256_hex)`` for an uploaded logo, or raise LogoError."""
    if not data:
        raise LogoError("No logo file was received.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise LogoError("Logo is too large. The limit is 1 MB.", status=413)
    # Decide on content, never on the file name or Content-Type.
    if not data.startswith(_PNG_SIGNATURE):
        raise LogoError("Logo must be a PNG image.", status=415)

    try:
        img = Image.open(io.BytesIO(data), formats=["PNG"])
        width, height = img.size
        # Checked from the header, before any pixel data is decoded.
        if width > MAX_EDGE or height > MAX_EDGE or width * height > _MAX_PIXELS:
            raise LogoError(f"Logo is too big. The limit is {MAX_EDGE}x{MAX_EDGE} pixels.")
        if width < MIN_EDGE or height < MIN_EDGE:
            raise LogoError(f"Logo is too small. It must be at least {MIN_EDGE}x{MIN_EDGE} pixels.")
        img.load()
        img = img.convert("RGBA")
    except LogoError:
        raise
    except Exception:
        # Truncated or corrupt file, decompression bomb, not really a PNG.
        raise LogoError("That file could not be read as a PNG image.")

    scale = min(STORED_EDGE / width, STORED_EDGE / height)
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    if new_size != img.size:
        img = img.resize(new_size, Image.LANCZOS)
    canvas = Image.new("RGBA", (STORED_EDGE, STORED_EDGE), (0, 0, 0, 0))
    canvas.paste(img, ((STORED_EDGE - new_size[0]) // 2, (STORED_EDGE - new_size[1]) // 2))

    png = _encode(canvas)
    if len(png) > MAX_STORED_BYTES:
        # Photo-like art: fall back to a 256-colour palette, which keeps alpha.
        png = _encode(canvas.quantize(colors=256, method=Image.Quantize.FASTOCTREE))
    if len(png) > MAX_STORED_BYTES:
        raise LogoError("Logo is too detailed to store. Use a simpler image with fewer colours.")

    return png, hashlib.sha256(png).hexdigest()
