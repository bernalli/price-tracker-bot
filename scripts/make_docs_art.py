#!/usr/bin/env python3
"""Regenerate the artwork shipped in ``docs/img/``.

Three images live in the README and in the repository's social preview. They are
generated here rather than drawn by hand so that a change of repository URL, of
tagline, or of chart style is a one-line edit followed by a re-run:

    uv run python scripts/make_docs_art.py

* ``price-chart.png`` — the ``/history`` output. It is rendered through the bot's
  own ``_render_chart`` so the README always shows what the code actually draws,
  and it is fed a **synthetic** series: the screenshot must never expose a real
  product, a real price history, or a real target from anyone's deployment.
* ``cover.png`` / ``social-preview.png`` — the banner and the 2:1 social card.

Only Pillow and matplotlib are needed; both are already runtime dependencies.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
IMG_DIR = REPO_ROOT / "docs" / "img"

REPO_URL = "github.com/bernalli/price-tracker-bot"
TITLE = "price-tracker-bot"
TAGLINE = "Self-hosted Telegram bot for multi-site price tracking"
CHIPS = ("auto-quarantine", "plugin scrapers", "Prometheus metrics")

YELLOW = "#FFD500"
GLOW = "#FFE04D"
INK = "#1A1A1A"
MUTED = "#6B6B6B"
RED = "#E01B24"

# A made-up listing: generic name, invented series, invented target.
DEMO_NAME = "Wireless Headphones XZ-900 — example-store.com"
DEMO_TARGET = 320.0
DEMO_SERIES = [
    351,
    347,
    349,
    353,
    356,
    358,
    355,
    352,
    354,
    351,
    349,
    350,
    345,
    344,
    377,
    370,
    374,
    371,
    375,
    378,
    373,
    372,
    376,
    381,
    380,
    355,
    354,
    353,
    357,
    355,
    354,
    357,
    356,
    355,
    352,
    347,
    347,
    294,
    293,
    295,
    293,
    292,
    294,
    292,
    291,
    296,
    298,
    295,
    297,
    296,
    299,
    301,
]


def _font(
    size: int, *, bold: bool = False, mono: bool = False
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Best-effort font lookup, falling back to Pillow's bundled default."""
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        ]
        if mono
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def render_chart(out: Path) -> None:
    """Render the demo chart through the bot's own chart function."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from price_tracker.bot.handlers.history import _render_chart  # noqa: PLC0415

    start = datetime(2026, 5, 8, tzinfo=UTC)
    dates = [start + timedelta(days=i) for i in range(len(DEMO_SERIES))]
    buf = _render_chart(dates, [float(p) for p in DEMO_SERIES], DEMO_TARGET, DEMO_NAME)
    out.write_bytes(buf.getvalue())


def _bag(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int) -> None:
    """Draw the shopping-bag mark with its discount slash and lightning badge."""
    half = size // 2
    body = (cx - half, cy - half + size // 8, cx + half, cy + half + size // 8)
    draw.rounded_rectangle(body, radius=size // 6, fill=INK)

    handle_w = int(size * 0.56)
    lw = max(4, size // 11)
    draw.arc(
        (cx - handle_w // 2, cy - half - size // 4, cx + handle_w // 2, cy - half + size // 3),
        start=180,
        end=360,
        fill="#4A4A4A",
        width=lw,
    )

    r = size // 9
    off = size // 5
    for dx, dy in ((-off, -off), (off, off)):
        draw.ellipse(
            (cx + dx - r, cy + dy - r + size // 8, cx + dx + r, cy + dy + r + size // 8),
            outline=YELLOW,
            width=max(3, size // 22),
        )
    draw.line(
        (cx - off - r // 2, cy + off + size // 8, cx + off + r // 2, cy - off + size // 8),
        fill=YELLOW,
        width=max(4, size // 16),
    )

    br = size // 5
    bx, by = cx + half - br // 3, cy - half + size // 10
    draw.ellipse((bx - br, by - br, bx + br, by + br), fill=RED)
    bolt = [
        (bx + br // 5, by - br // 2),
        (bx - br // 3, by + br // 12),
        (bx, by + br // 12),
        (bx - br // 5, by + br // 2),
        (bx + br // 3, by - br // 12),
        (bx, by - br // 12),
    ]
    draw.polygon(bolt, fill=YELLOW)


def render_banner(out: Path, width: int, height: int, *, scale: float) -> None:
    """Draw a cover/social card at the given size."""
    img = Image.new("RGB", (width, height), YELLOW)
    draw = ImageDraw.Draw(img)

    mark_cx = int(width * 0.80)
    mark_cy = height // 2
    glow_r = int(min(width, height) * 0.37)
    draw.ellipse(
        (mark_cx - glow_r, mark_cy - glow_r, mark_cx + glow_r, mark_cy + glow_r), fill=GLOW
    )
    for dx, dy, s in ((-glow_r - 30, -30, 11), (glow_r + 10, 0, 9), (-glow_r + 40, glow_r - 20, 9)):
        px, py = mark_cx + dx, mark_cy + dy
        draw.line((px - s, py, px + s, py), fill="#E6BF00", width=4)
        draw.line((px, py - s, px, py + s), fill="#E6BF00", width=4)
    _bag(draw, mark_cx, mark_cy, int(min(width, height) * 0.42))

    x = int(width * 0.055)
    title_f = _font(int(60 * scale), bold=True)
    tag_f = _font(int(26 * scale))
    chip_f = _font(int(19 * scale), mono=True)
    url_f = _font(int(19 * scale), mono=True)

    y = int(height * 0.28)
    draw.text((x, y), TITLE, font=title_f, fill=INK)
    y += int(78 * scale)
    draw.text((x, y), TAGLINE, font=tag_f, fill=INK)

    y += int(48 * scale)
    cx = x
    for chip in CHIPS:
        w = int(draw.textlength(chip, font=chip_f))
        pad = int(16 * scale)
        h = int(34 * scale)
        draw.rounded_rectangle((cx, y, cx + w + 2 * pad, y + h), radius=h // 2, fill=GLOW)
        draw.text((cx + pad, y + h // 2), chip, font=chip_f, fill=MUTED, anchor="lm")
        cx += w + 2 * pad + int(14 * scale)

    draw.text((x, int(height * 0.87)), REPO_URL, font=url_f, fill=MUTED)
    img.save(out, "PNG", optimize=True)


def main() -> None:
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    render_chart(IMG_DIR / "price-chart.png")
    render_banner(IMG_DIR / "cover.png", 1600, 400, scale=1.0)
    render_banner(IMG_DIR / "social-preview.png", 1280, 640, scale=1.05)
    for name in ("price-chart.png", "cover.png", "social-preview.png"):
        print(f"wrote {IMG_DIR / name}")


if __name__ == "__main__":
    main()
