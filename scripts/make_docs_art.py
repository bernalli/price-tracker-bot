#!/usr/bin/env python3
"""Regenerate the artwork shipped in ``docs/img/``.

``price-chart.png`` uses the bot's chart renderer with a synthetic price series.
The other brand images are rendered from SVG with ``rsvg-convert``. Install it,
Fontconfig (``fc-match``), and the Lato Regular, Bold and Black fonts before
running:

    uv run python scripts/make_docs_art.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
IMG_DIR = REPO_ROOT / "docs" / "img"
LOGO_DIR = IMG_DIR / "logo"

YELLOW = "#FFD60A"
DARK = "#1C1C1C"
MUTED = "#4A3F00"
FONT = "Lato"

ALERT_NAME = "Noise-cancelling headphones"
ALERT_OLD = "129.00"
ALERT_NEW = "99.00"
ALERT_DROP = "30.00"
ALERT_PERCENT = "23.3"
ALERT_SYMBOL = "€"

# The measured transform centres the visible bag before fitting the complete mark.
LOGO_ART = """
<g transform="translate(256 256) scale(0.9032) translate(-236 -259)">
  <path d="M178.4,206 V172.96 a57.6,57.6 0 0 1 115.2,0 V206"
        fill="none" stroke="#1C1C1C" stroke-width="26" stroke-linecap="round"/>
  <rect x="116" y="196" width="240" height="220" rx="36" fill="#1C1C1C"/>
  <circle cx="192" cy="262" r="22" fill="none" stroke="#FFD60A" stroke-width="14"/>
  <circle cx="280" cy="350" r="22" fill="none" stroke="#FFD60A" stroke-width="14"/>
  <line x1="296" y1="244" x2="176" y2="368" stroke="#FFD60A"
        stroke-width="18" stroke-linecap="round"/>
  <polygon points="372,102 392.2,118.7 418,114.3 427.2,138.8 451.7,148
                   447.3,173.8 464,194 447.3,214.2 451.7,240 427.2,249.2
                   418,273.7 392.2,269.3 372,286 351.8,269.3 326,273.7
                   316.8,249.2 292.3,240 296.7,214.2 280,194 296.7,173.8
                   292.3,148 316.8,138.8 326,114.3 351.8,118.7"
           fill="#FFD60A" stroke="#FFD60A" stroke-width="10" stroke-linejoin="round"/>
  <polygon points="372,118 388,134.1 410,128.2 415.8,150.2 437.8,156
                   431.9,178 448,194 431.9,210 437.8,232 415.8,237.8
                   410,259.8 388,253.9 372,270 356,253.9 334,259.8
                   328.2,237.8 306.2,232 312.1,210 296,194 312.1,178
                   306.2,156 328.2,150.2 334,128.2 356,134.1"
           fill="#E11D48" stroke="#E11D48" stroke-width="10" stroke-linejoin="round"/>
  <path d="M354.84,158 H389.16 V193.64 H406.32 L372,231.92
           L337.68,193.64 H354.84 Z" fill="#FFFFFF"/>
</g>
"""

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


def render_chart(out: Path) -> None:
    """Render the synthetic demo data through the bot's chart function."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from price_tracker.bot.handlers.history import _render_chart  # noqa: PLC0415

    start = datetime(2026, 5, 8, tzinfo=UTC)
    dates = [start + timedelta(days=i) for i in range(len(DEMO_SERIES))]
    buf = _render_chart(dates, [float(price) for price in DEMO_SERIES], DEMO_TARGET, DEMO_NAME)
    out.write_bytes(buf.getvalue())


def logo(x: float, y: float, size: float) -> str:
    """Place the statically centred brand mark in an SVG composition."""
    return (
        f'<svg x="{x}" y="{y}" width="{size}" height="{size}" '
        f'viewBox="0 0 512 512">{LOGO_ART}</svg>'
    )


def _chart_emoji(x: float, y: float, font_size: float) -> str:
    """Draw a small falling chart in place of the colour emoji glyph."""
    size = font_size * 1.15
    grid = "".join(
        f'<line x1="{x + size * step / 4}" y1="{y}" '
        f'x2="{x + size * step / 4}" y2="{y + size}" '
        f'stroke="#C9D7E8" stroke-width="{font_size * 0.05}"/>'
        f'<line x1="{x}" y1="{y + size * step / 4}" '
        f'x2="{x + size}" y2="{y + size * step / 4}" '
        f'stroke="#C9D7E8" stroke-width="{font_size * 0.05}"/>'
        for step in (1, 2, 3)
    )
    return (
        f'<rect x="{x}" y="{y}" width="{size}" height="{size}" rx="{size * 0.14}" '
        f'fill="#FFFFFF" stroke="#B8C4D2" stroke-width="{font_size * 0.06}"/>{grid}'
        f'<polyline points="{x + size * 0.12},{y + size * 0.2} '
        f"{x + size * 0.4},{y + size * 0.5} "
        f"{x + size * 0.58},{y + size * 0.38} "
        f'{x + size * 0.88},{y + size * 0.8}" fill="none" stroke="#E53935" '
        f'stroke-width="{font_size * 0.13}" stroke-linecap="round" '
        'stroke-linejoin="round"/>'
    )


def alert_bubble(x: float, y: float, width: float, font_size: float) -> str:
    """Draw the price-drop message defined by ``core.alert.format_alert``."""
    line_height = font_size * 1.45
    rows = [
        ('<tspan font-weight="bold">Price drop!</tspan>', DARK),
        ("", DARK),
        (f'<tspan font-weight="bold">{ALERT_NAME}</tspan>', DARK),
        ("View product", "#2A7FC1"),
        ("", DARK),
        (
            f'Was: <tspan text-decoration="line-through">{ALERT_OLD} {ALERT_SYMBOL}</tspan>',
            DARK,
        ),
        (f'Now: <tspan font-weight="bold">{ALERT_NEW} {ALERT_SYMBOL}</tspan>', DARK),
        (f"Drop: -{ALERT_DROP} {ALERT_SYMBOL} ({ALERT_PERCENT}%)", DARK),
    ]
    padding = font_size * 1.1
    height = padding * 2 + line_height * len(rows) + font_size * 0.6
    parts = [
        f'<rect x="{x + 6}" y="{y + 8}" width="{width}" height="{height}" '
        f'rx="{font_size * 1.1}" fill="#000000" opacity="0.10"/>',
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
        f'rx="{font_size * 1.1}" fill="#FFFFFF"/>',
        f'<path d="M{x},{y + height - font_size * 1.6} '
        f"q-{font_size * 0.2},{font_size * 1.4} -{font_size * 0.9},{font_size * 1.6} "
        f'q{font_size * 1.4},{font_size * 0.2} {font_size * 2.2},-{font_size * 0.6} z" '
        'fill="#FFFFFF"/>',
        _chart_emoji(x + padding, y + padding, font_size),
    ]
    emoji_size = font_size * 1.15

    for index, (text, colour) in enumerate(rows):
        if not text:
            continue
        text_x = x + padding + (emoji_size + font_size * 0.35 if index == 0 else 0)
        parts.append(
            f'<text x="{text_x}" y="{y + padding + font_size + index * line_height}" '
            f'font-family="{FONT}" font-size="{font_size}" fill="{colour}">{text}</text>'
        )

    parts.append(
        f'<text x="{x + width - padding}" y="{y + height - padding * 0.6}" '
        f'text-anchor="end" font-family="{FONT}" font-size="{font_size * 0.7}" '
        'fill="#8A8A8A">09:41</text>'
    )
    return "".join(parts)


def svg_document(width: int, height: int, body: str) -> str:
    """Wrap a composition in the yellow brand canvas."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}"><rect width="{width}" height="{height}" '
        f'fill="{YELLOW}"/>{body}</svg>\n'
    )


def cover_svg() -> str:
    """Return the 1600×400 README cover."""
    return svg_document(
        1600,
        400,
        logo(70, 70, 260) + f'<text x="360" y="178" font-family="{FONT}" font-weight="900" '
        f'font-size="74" fill="{DARK}">price-tracker-bot</text>'
        + f'<text x="362" y="232" font-family="{FONT}" font-size="31" '
        f'fill="{DARK}">Send a link. Get a message when the price drops.</text>'
        + f'<text x="362" y="282" font-family="{FONT}" font-size="21" '
        f'fill="{MUTED}">Self-hosted Telegram bot · open source (MIT) · one Docker container</text>'
        + alert_bubble(1150, 62, 360, 20),
    )


def social_preview_svg() -> str:
    """Return the 1280×640 repository social preview."""
    return svg_document(
        1280,
        640,
        logo(80, 120, 300) + f'<text x="80" y="500" font-family="{FONT}" font-weight="900" '
        f'font-size="68" fill="{DARK}">price-tracker-bot</text>'
        + f'<text x="82" y="552" font-family="{FONT}" font-size="30" '
        f'fill="{DARK}">Send a link. Get a message when the price drops.</text>'
        + alert_bubble(740, 110, 440, 25)
        + f'<text x="1200" y="590" text-anchor="end" font-family="{FONT}" '
        f'font-size="20" fill="{MUTED}">github.com/bernalli/price-tracker-bot</text>',
    )


def telegram_description_svg() -> str:
    """Return the 640×360 Telegram bot description image."""
    return svg_document(
        640,
        360,
        logo(40, 60, 190)
        + alert_bubble(268, 46, 320, 16)
        + f'<text x="320" y="330" text-anchor="middle" font-family="{FONT}" '
        f'font-size="22" fill="{DARK}">Send a link. Get a message when the price drops.</text>',
    )


def render_svg(renderer: str, source: str, output: Path, width: int) -> None:
    """Render one in-memory SVG to a PNG file."""
    subprocess.run(
        [renderer, "-w", str(width), "-o", str(output), "-"],
        input=source.encode(),
        check=True,
    )


def render_svg_supersampled(
    renderer: str,
    source: str,
    output: Path,
    width: int,
    height: int,
) -> None:
    """Render an SVG at 2× and downsample it to the requested PNG size."""
    rendered = subprocess.run(
        [renderer, "-w", str(width * 2), "-"],
        input=source.encode(),
        capture_output=True,
        check=True,
    )
    with Image.open(BytesIO(rendered.stdout)) as image:
        image.resize((width, height), Image.Resampling.LANCZOS).save(output, optimize=True)


def require_fonts() -> None:
    """Reject missing font faces instead of silently rendering substitutes."""
    matcher = shutil.which("fc-match")
    if matcher is None:
        raise SystemExit("fc-match (Fontconfig) is required to verify the artwork fonts")
    for weight, style in (("regular", "Regular"), ("bold", "Bold"), ("black", "Black")):
        match = subprocess.run(
            [matcher, "--format", "%{family}|%{style}", f"{FONT}:weight={weight}"],
            capture_output=True,
            text=True,
            check=True,
        )
        families, _, styles = match.stdout.strip().partition("|")
        if FONT not in families.split(",") or style not in styles.split(","):
            raise SystemExit(f"{FONT} {style} is required to render the documentation images")


def main() -> None:
    """Regenerate every documentation image."""
    renderer = shutil.which("rsvg-convert")
    if renderer is None:
        raise SystemExit("rsvg-convert is required to render the documentation images")

    require_fonts()
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    render_chart(IMG_DIR / "price-chart.png")
    images = (
        ("cover.png", cover_svg(), 3200),
        ("social-preview.png", social_preview_svg(), 2560),
    )
    for name, source, width in images:
        output = IMG_DIR / name
        render_svg(renderer, source, output, width)
        print(f"wrote {output}")
    telegram_output = IMG_DIR / "telegram-description.png"
    render_svg_supersampled(
        renderer,
        telegram_description_svg(),
        telegram_output,
        640,
        360,
    )
    print(f"wrote {telegram_output}")
    logos = (
        ("icon.svg", "icon-1024.png", 1024),
        ("avatar.svg", "avatar-1280.png", 1280),
    )
    for source_name, output_name, width in logos:
        output = LOGO_DIR / output_name
        render_svg(
            renderer,
            (LOGO_DIR / source_name).read_text(encoding="utf-8"),
            output,
            width,
        )
        print(f"wrote {output}")
    print(f"wrote {IMG_DIR / 'price-chart.png'}")


if __name__ == "__main__":
    main()
