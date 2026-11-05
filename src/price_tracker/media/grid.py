"""Render up to six cached thumbnails as a numbered product grid."""

from __future__ import annotations

import io
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

import matplotlib
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from price_tracker.media.decode import THUMB_SIDE

CANVAS: Final = (960, 800)
CELL: Final = (320, 400)
PAD: Final = 10
IMAGE_SIDE: Final = 300
BACKGROUND: Final = "#EDEDED"
_TILE_BACKGROUND: Final = "#FFFFFF"
_TEXT: Final = "#111111"
_NEUTRAL: Final = "#E3E3E3"
_ICON: Final = "#A8A8A8"
_BADGE: Final = "#1F1F1F"


@dataclass(frozen=True, slots=True)
class Tile:
    """One grid cell: its list number, the price label and the cached thumbnail bytes, if any."""

    number: int
    price: str
    thumb: bytes | None


@lru_cache
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(
            Path(matplotlib.get_data_path()) / "fonts/ttf/DejaVuSans-Bold.ttf", size
        )
    except OSError:
        return ImageFont.load_default(size=size)


def _placeholder(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
    draw.rectangle(box, fill=_NEUTRAL)
    cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    radius = min(box[2] - box[0], box[3] - box[1]) // 5
    draw.rectangle(
        (cx - radius, cy - radius // 2, cx + radius, cy + radius // 2), outline=_ICON, width=4
    )
    draw.line((cx - radius, cy - radius // 2, cx, cy), fill=_ICON, width=4)
    draw.line((cx + radius, cy - radius // 2, cx, cy), fill=_ICON, width=4)


def _thumbnail(data: bytes | None) -> Image.Image | None:
    if data is None:
        return None
    try:
        with Image.open(io.BytesIO(data), formats=("JPEG",)) as image:
            if image.size != (THUMB_SIDE, THUMB_SIDE):
                return None
            image.load()
            return image.convert("RGB").resize((IMAGE_SIDE, IMAGE_SIDE), Image.Resampling.LANCZOS)
    except (
        OSError,
        ValueError,
        SyntaxError,
        EOFError,
        struct.error,
        IndexError,
        TypeError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return None


def _price_label(price: str) -> tuple[str, ImageFont.FreeTypeFont | ImageFont.ImageFont]:
    price = " ".join(price.splitlines())
    for size in range(30, 15, -1):
        font = _font(size)
        if font.getlength(price) <= IMAGE_SIDE:
            return price, font
    low, high = 0, len(price)
    while low < high:
        middle = (low + high + 1) // 2
        if font.getlength(price[:middle] + "…") <= IMAGE_SIDE:
            low = middle
        else:
            high = middle - 1
    return price[:low] + "…", font


def render_grid(tiles: Sequence[Tile]) -> bytes:
    if not isinstance(tiles, Sequence) or len(tiles) > 6:
        raise ValueError("invalid grid tiles")
    numbers = set()
    for tile in tiles:
        if (
            not isinstance(tile, Tile)
            or not isinstance(tile.number, int)
            or isinstance(tile.number, bool)
            or not 1 <= tile.number <= 6
            or tile.number in numbers
            or not isinstance(tile.price, str)
            or not (tile.thumb is None or isinstance(tile.thumb, bytes))
        ):
            raise ValueError("invalid grid tiles")
        numbers.add(tile.number)
    canvas = Image.new("RGB", CANVAS, BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    if not tiles:
        _placeholder(draw, (330, 250, 630, 550))
    for index, tile in enumerate(tiles):
        x, y = (index % 3) * CELL[0], (index // 3) * CELL[1]
        draw.rectangle((x, y, x + CELL[0] - 1, y + CELL[1] - 1), fill=_TILE_BACKGROUND)
        thumb = _thumbnail(tile.thumb)
        if thumb is None:
            _placeholder(
                draw, (x + PAD, y + PAD, x + PAD + IMAGE_SIDE - 1, y + PAD + IMAGE_SIDE - 1)
            )
        else:
            canvas.paste(thumb, (x + PAD, y + PAD))
            thumb.close()
        draw.ellipse((x + 14, y + 14, x + 62, y + 62), fill=_BADGE)
        draw.text(
            (x + 38, y + 38), str(tile.number), font=_font(28), fill=_TILE_BACKGROUND, anchor="mm"
        )
        label, font = _price_label(tile.price)
        draw.text((x + CELL[0] // 2, y + 355), label, font=font, fill=_TEXT, anchor="mm")
    output = io.BytesIO()
    canvas.save(output, "JPEG", quality=85)
    canvas.close()
    return output.getvalue()
