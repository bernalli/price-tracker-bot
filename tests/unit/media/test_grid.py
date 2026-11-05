"""Product grid rendering and rejection before any pixel allocation."""

from __future__ import annotations

import io
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from price_tracker.media import grid
from price_tracker.media.grid import Tile, render_grid
from tests.support.images import jpeg_bytes, png_bytes


def image_of(data: bytes) -> Image.Image:
    assert data.startswith(b"\xff\xd8\xff")
    assert len(data) <= 10_000_000
    image = Image.open(io.BytesIO(data))
    image.load()
    assert image.format == "JPEG"
    assert image.size == (960, 800)
    assert image.width + image.height <= 10000
    assert max(image.size) / min(image.size) <= 20
    return image


def pixel_at(image: Image.Image, position: tuple[int, int]) -> tuple[int, ...]:
    pixel = image.getpixel(position)
    assert isinstance(pixel, tuple)
    return pixel


@pytest.mark.parametrize("count", range(7))
def test_zero_to_six_tiles_make_a_valid_telegram_photo(count: int) -> None:
    with image_of(render_grid([Tile(n + 1, "$12.00", None) for n in range(count)])) as image:
        if count == 0:
            assert pixel_at(image, (480, 400)) != pixel_at(image, (0, 0))
        if count < 6:
            assert all(abs(c - 237) < 4 for c in pixel_at(image, (950, 790)))


def test_tiles_are_row_major_with_a_dark_number_badge() -> None:
    with image_of(
        render_grid(
            [
                Tile(1, "$1", jpeg_bytes(320, 320, "red")),
                Tile(2, "$2", None),
                Tile(3, "$3", jpeg_bytes(320, 320, "blue")),
                Tile(4, "$4", jpeg_bytes(320, 320, "green")),
            ]
        )
    ) as image:
        red = pixel_at(image, (160, 160))
        assert red[0] > 200
        assert red[1] < 40
        assert red[2] < 40
        assert all(abs(c - 227) < 4 for c in pixel_at(image, (400, 80)))
        blue = pixel_at(image, (800, 160))
        assert blue[2] > 200
        assert blue[0] < 40
        green = pixel_at(image, (160, 560))
        assert green[1] > 90
        assert green[0] < 40
        assert all(c < 60 for c in pixel_at(image, (22, 38)))


@pytest.mark.parametrize(
    "thumb",
    [
        None,
        b"bad",
        b"\xff\xd8\xff",
        png_bytes(320, 320),
        jpeg_bytes(100, 100),
        jpeg_bytes(640, 640),
        jpeg_bytes(320, 320)[:30],
    ],
)
def test_missing_corrupt_wrong_format_and_wrong_size_thumbnails_are_neutral(
    thumb: bytes | None,
) -> None:
    with image_of(render_grid([Tile(1, "EUR 1", thumb)])) as image:
        assert all(abs(c - 227) < 4 for c in pixel_at(image, (80, 80)))


@pytest.mark.parametrize(
    "tiles",
    [
        None,
        1,
        {},
        iter([]),
        [None],
        [1],
        [Tile(n, "x", None) for n in range(1, 8)],
        [Tile(1, "x", None), Tile(1, "x", None)],
    ],
)
def test_invalid_sequences_fail_before_allocating_pixels(
    tiles: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid tiles must be rejected before rendering")

    monkeypatch.setattr(Image, "new", forbidden)
    with pytest.raises(ValueError, match="invalid grid tiles"):
        render_grid(tiles)


@pytest.mark.parametrize("number", [0, 7, -1, True, False, 1.0, "1", None])
def test_invalid_numbers_fail_before_rendering(
    number: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid numbers must be rejected before rendering")

    monkeypatch.setattr(Image, "new", forbidden)
    with pytest.raises(ValueError, match="invalid grid tiles"):
        render_grid([Tile(number, "price", None)])


@pytest.mark.parametrize(
    ("price", "thumb"),
    [(None, None), (1, None), (False, None), ("x", "jpeg"), ("x", bytearray(b"x")), ("x", 1)],
)
def test_wrong_field_types_fail_before_rendering(
    price: Any, thumb: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid fields must be rejected before rendering")

    monkeypatch.setattr(Image, "new", forbidden)
    with pytest.raises(ValueError, match="invalid grid tiles"):
        render_grid([Tile(1, price, thumb)])


@given(st.lists(st.text(max_size=200), max_size=6))
@settings(max_examples=50, deadline=None)
def test_arbitrary_unicode_prices_always_render(prices: list[str]) -> None:
    with image_of(
        render_grid([Tile(index + 1, price, None) for index, price in enumerate(prices)])
    ):
        pass


@pytest.mark.parametrize("price", ["\n\r\t", "😀" * 200, "السعر ١٢٣", "£1234567890" * 30, ""])
def test_hostile_text_and_long_prices_are_bounded(price: str) -> None:
    with image_of(render_grid([Tile(1, price, None)])):
        pass


def test_prices_shrink_from_30_to_16_px_then_truncate_within_the_image_width() -> None:
    label, font = grid._price_label("$12.00")
    assert (label, font.size) == ("$12.00", 30)
    long_price = "£1234567890" * 30
    label, font = grid._price_label(long_price)
    assert font.size == 16
    assert label.endswith("…")
    assert long_price.startswith(label[:-1])
    assert font.getlength(label) <= grid.IMAGE_SIDE
    assert font.getlength(label[:-1] + long_price[len(label) - 1] + "…") > grid.IMAGE_SIDE


def test_missing_bundled_font_uses_the_default(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    grid._font.cache_clear()
    monkeypatch.setattr("price_tracker.media.grid.matplotlib.get_data_path", lambda: str(tmp_path))
    try:
        with image_of(render_grid([Tile(1, "£12 😀", None)])):
            pass
    finally:
        grid._font.cache_clear()
