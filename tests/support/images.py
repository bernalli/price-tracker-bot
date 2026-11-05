"""Small in-memory image fixtures, including lazy dimension-only headers."""

import io
import struct
import zlib

from PIL import Image

EPS_HEADER = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n"


def _bytes(
    w: int, h: int, mode: str, fmt: str, color: str | int | tuple[int, ...] = "red"
) -> bytes:
    out = io.BytesIO()
    with Image.new(mode, (w, h), color) as image:
        image.save(out, fmt)
    return out.getvalue()


def jpeg_bytes(w: int = 100, h: int = 100, color: str = "red") -> bytes:
    return _bytes(w, h, "RGB", "JPEG", color)


def png_bytes(w: int = 100, h: int = 100, mode: str = "RGB") -> bytes:
    return _bytes(w, h, mode, "PNG", (0, 0, 0, 0) if mode == "RGBA" else 0)


def webp_bytes(w: int = 100, h: int = 100) -> bytes:
    return _bytes(w, h, "RGB", "WEBP")


def gif_bytes(frames: int = 2) -> bytes:
    out = io.BytesIO()
    images = [Image.new("RGB", (64, 64), "red" if i == 0 else "blue") for i in range(frames)]
    try:
        images[0].save(out, "GIF", save_all=True, append_images=images[1:], duration=50)
    finally:
        for image in images:
            image.close()
    return out.getvalue()


def png_with_declared_size(w: int, h: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00"))
        + chunk(b"IEND", b"")
    )


def tiff_bytes() -> bytes:
    return _bytes(32, 32, "RGB", "TIFF")


def bmp_bytes() -> bytes:
    return _bytes(32, 32, "RGB", "BMP")
