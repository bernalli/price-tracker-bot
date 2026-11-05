"""Decode untrusted images into bounded, metadata-free JPEG thumbnails."""

from __future__ import annotations

import io
import struct
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_FORMATS: Final = ("JPEG", "PNG", "WEBP", "GIF")
MAX_IMAGE_BYTES: Final = 8 * 1024 * 1024
MAX_SOURCE_PIXELS: Final = 25_000_000
MIN_SIDE: Final = 16
MAX_SIDE: Final = 16_000
MAX_ASPECT: Final = 10.0
THUMB_SIDE: Final = 320
JPEG_QUALITY: Final = 82
FALLBACK_QUALITY: Final = 60
MAX_THUMB_BYTES: Final = 128 * 1024

# The only place the process opens untrusted images: bound Pillow's own bomb guard
# (warning above this, error above twice this, i.e. above MAX_SOURCE_PIXELS) on top of
# the explicit check below.
Image.MAX_IMAGE_PIXELS = MAX_SOURCE_PIXELS // 2

_DECODE_ERRORS = (
    UnidentifiedImageError,
    OSError,
    SyntaxError,
    ValueError,
    EOFError,
    struct.error,
    IndexError,
    TypeError,
)
_BOMB_ERRORS = (Image.DecompressionBombError, Image.DecompressionBombWarning)


class ImageRejected(Exception):
    """The bytes are not an image this bot will turn into a thumbnail."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def make_thumbnail(data: bytes) -> bytes:
    """A THUMB_SIDE x THUMB_SIDE JPEG of the image in ``data``; raises ImageRejected otherwise."""
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_IMAGE_BYTES:
        raise ImageRejected("size")
    try:
        img = Image.open(io.BytesIO(data), formats=ALLOWED_FORMATS)
    except _BOMB_ERRORS as exc:
        raise ImageRejected("decompression bomb") from exc
    except _DECODE_ERRORS as exc:
        raise ImageRejected("not an image") from exc
    try:
        if img.format not in ALLOWED_FORMATS:
            raise ImageRejected("format")
        w, h = img.size
        if (
            min(w, h) < MIN_SIDE
            or max(w, h) > MAX_SIDE
            or w * h > MAX_SOURCE_PIXELS
            or max(w, h) / min(w, h) > MAX_ASPECT
        ):
            raise ImageRejected("dimensions")
        try:
            if img.format == "GIF":
                img.seek(0)
            if img.format == "JPEG":
                img.draft("RGB", (THUMB_SIDE, THUMB_SIDE))
            img.load()
        except _BOMB_ERRORS as exc:
            raise ImageRejected("decompression bomb") from exc
        except _DECODE_ERRORS as exc:
            raise ImageRejected("corrupt") from exc
        try:
            rotated = ImageOps.exif_transpose(img)
        except (OSError, ValueError, KeyError, TypeError, struct.error):
            rotated = img
        try:
            if rotated.mode in {"RGBA", "LA"} or (
                rotated.mode == "P" and "transparency" in rotated.info
            ):
                rgba = rotated.convert("RGBA")
                rgb = Image.new("RGB", rgba.size, "white")
                rgb.paste(rgba, mask=rgba.getchannel("A"))
            else:
                rgb = rotated.convert("RGB")
        except (ValueError, OSError) as exc:
            raise ImageRejected("mode") from exc
        small = ImageOps.contain(rgb, (THUMB_SIDE, THUMB_SIDE), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (THUMB_SIDE, THUMB_SIDE), "white")
        canvas.paste(small, ((THUMB_SIDE - small.width) // 2, (THUMB_SIDE - small.height) // 2))
        for quality in (JPEG_QUALITY, FALLBACK_QUALITY):
            buf = io.BytesIO()
            canvas.save(buf, "JPEG", quality=quality, optimize=True)
            result = buf.getvalue()
            if len(result) <= MAX_THUMB_BYTES:
                return result
        raise ImageRejected("thumbnail too large")
    finally:
        img.close()
