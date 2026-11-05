"""Untrusted image decoding is bounded before pixel allocation and strips metadata."""

from __future__ import annotations

import io
import struct
import subprocess
import warnings
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from price_tracker.media import decode
from price_tracker.media.decode import ImageRejected, make_thumbnail
from tests.support.images import (
    EPS_HEADER,
    bmp_bytes,
    gif_bytes,
    jpeg_bytes,
    png_bytes,
    png_with_declared_size,
    tiff_bytes,
    webp_bytes,
)


def assert_thumbnail(data: bytes) -> None:
    assert data.startswith(b"\xff\xd8\xff")
    assert len(data) <= decode.MAX_THUMB_BYTES
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        assert image.size == (320, 320)
        assert image.mode == "RGB"
        assert image.format == "JPEG"
        assert not image.getexif()
        assert not image.info.get("icc_profile")


@pytest.mark.parametrize("source", [jpeg_bytes(), png_bytes(), webp_bytes(), gif_bytes()])
def test_supported_images_become_small_clean_square_jpegs(source: bytes) -> None:
    result = make_thumbnail(source)
    assert result != source
    assert_thumbnail(result)


def test_transparency_is_composited_on_white() -> None:
    result = make_thumbnail(png_bytes(mode="RGBA"))
    with Image.open(io.BytesIO(result)) as image:
        assert image.getpixel((160, 160)) == (255, 255, 255)
        assert image.getpixel((0, 0)) == (255, 255, 255)


def test_only_the_first_gif_frame_is_used() -> None:
    with Image.open(io.BytesIO(make_thumbnail(gif_bytes()))) as image:
        pixel = image.getpixel((160, 160))
        assert isinstance(pixel, tuple)
        red, green, blue = pixel
        assert red > 200
        assert green < 30
        assert blue < 30


def test_cmyk_and_sixteen_bit_modes_are_safe() -> None:
    out = io.BytesIO()
    Image.new("CMYK", (64, 64)).save(out, "JPEG")
    assert_thumbnail(make_thumbnail(out.getvalue()))
    try:
        result = make_thumbnail(png_bytes(64, 64, "I;16"))
    except ImageRejected:
        pass
    else:
        assert_thumbnail(result)


@pytest.mark.parametrize("data", [b"", b"x" * (8 * 1024 * 1024 + 1), None, bytearray(b"x"), "bad"])
def test_invalid_input_sizes_and_types_are_refused(data: Any) -> None:
    with pytest.raises(ImageRejected, match="size"):
        make_thumbnail(data)


@pytest.mark.parametrize(("w", "h"), [(6000, 5000), (8000, 8000), (12000, 12000), (4901, 5101)])
@pytest.mark.parametrize("policy", ["always", "error"])
def test_decompression_bombs_are_refused_before_loading(w: int, h: int, policy: Any) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter(policy, Image.DecompressionBombWarning)
        with pytest.raises(ImageRejected, match="decompression bomb"):
            make_thumbnail(png_with_declared_size(w, h))


@pytest.mark.parametrize(("w", "h"), [(20000, 100), (16, 16000), (8, 8)])
def test_invalid_dimensions_are_refused_before_loading(w: int, h: int) -> None:
    with pytest.raises(ImageRejected, match="dimensions"):
        make_thumbnail(png_with_declared_size(w, h))


@pytest.mark.parametrize("policy", ["always", "error"])
def test_exactly_twenty_five_megapixels_obey_the_callers_warning_policy(policy: Any) -> None:
    data = png_bytes(5000, 5000)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter(policy, Image.DecompressionBombWarning)
        if policy == "error":
            with pytest.raises(ImageRejected, match="decompression bomb"):
                make_thumbnail(data)
        else:
            assert_thumbnail(make_thumbnail(data))
            assert any(isinstance(item.message, Image.DecompressionBombWarning) for item in caught)
    assert Image.MAX_IMAGE_PIXELS == decode.MAX_SOURCE_PIXELS // 2


def webp_header(kind: bytes, w: int, h: int) -> bytes:
    if kind == b"VP8 ":
        body = b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", w, h)
    elif kind == b"VP8L":
        body = b"\x2f" + struct.pack("<I", (w - 1) | ((h - 1) << 14))
    else:
        body = b"\x00" * 4 + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    return (
        b"RIFF"
        + struct.pack("<I", 12 + len(body))
        + b"WEBP"
        + kind
        + struct.pack("<I", len(body))
        + body
    )


@pytest.mark.parametrize("kind", [b"VP8 ", b"VP8L", b"VP8X"])
@pytest.mark.parametrize(("w", "h"), [(6000, 5000), (4901, 5101)])
def test_webp_dimensions_are_checked_before_the_native_decoder(
    kind: bytes, w: int, h: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("the native decoder must not receive a decompression bomb")

    monkeypatch.setattr("PIL.WebPImagePlugin._webp.WebPAnimDecoder", forbidden)
    with pytest.raises(ImageRejected, match="decompression bomb"):
        make_thumbnail(webp_header(kind, w, h))


def test_exact_webp_pixel_limit_reaches_the_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    def decoder(*args: Any, **kwargs: Any) -> Any:
        called.append(True)
        raise OSError("incomplete image")

    monkeypatch.setattr("PIL.WebPImagePlugin._webp.WebPAnimDecoder", decoder)
    with pytest.raises(ImageRejected, match="not an image"):
        make_thumbnail(webp_header(b"VP8X", 5000, 5000))
    assert called == [True]


@pytest.mark.parametrize(
    "source", [bmp_bytes(), tiff_bytes(), b'<svg xmlns="http://www.w3.org/2000/svg"/>', EPS_HEADER]
)
def test_unsupported_formats_never_spawn_a_decoder_process(
    source: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("image decoding must never spawn a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(ImageRejected, match="not an image"):
        make_thumbnail(source)


@settings(max_examples=100, deadline=None)
@given(st.binary(max_size=2000))
def test_arbitrary_bytes_never_escape_as_unexpected_exceptions(data: bytes) -> None:
    try:
        result = make_thumbnail(data)
    except ImageRejected:
        pass
    else:
        assert_thumbnail(result)


@settings(max_examples=100, deadline=None)
@given(st.integers(0, len(jpeg_bytes()) - 1))
def test_truncated_jpegs_never_escape_as_unexpected_exceptions(offset: int) -> None:
    try:
        result = make_thumbnail(jpeg_bytes()[:offset])
    except ImageRejected:
        pass
    else:
        assert_thumbnail(result)


def test_corrupt_pixels_and_oversized_output_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ImageRejected, match="corrupt"):
        make_thumbnail(png_with_declared_size(64, 64))
    monkeypatch.setattr(decode, "MAX_THUMB_BYTES", 1)
    with pytest.raises(ImageRejected, match="thumbnail too large"):
        make_thumbnail(jpeg_bytes())


def test_source_exif_and_color_profiles_are_removed() -> None:
    out = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "Product image"
    Image.new("RGB", (64, 100), "red").save(
        out, "JPEG", exif=exif, icc_profile=b"untrusted profile"
    )
    assert_thumbnail(make_thumbnail(out.getvalue()))
