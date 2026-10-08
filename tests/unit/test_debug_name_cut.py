"""``/debug`` cuts the scraped name by display cells and says so with an ellipsis."""

from __future__ import annotations

import html
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from price_tracker.bot.handlers.debug import cmd_debug
from price_tracker.bot.ui.width import display_width

_PAGE = "<html><body><p>nothing here</p></body></html>"
_NAME_LINE = re.compile(r"^   Name: (.*)$", re.MULTILINE)


async def _debug_report(name: str | None) -> str:
    """Run ``/debug`` against a scraper that returns ``name`` and return the final report."""
    scraper = SimpleNamespace(
        scrape=AsyncMock(return_value=SimpleNamespace(name=name, price=None, error="e"))
    )
    registry = SimpleNamespace(resolve=lambda _url: scraper)
    db = MagicMock()
    db.is_user_admin = AsyncMock(return_value=True)
    db.is_user_allowed = AsyncMock(return_value=True)
    context = MagicMock()
    context.args = ["https://shop.example/p/1"]
    context.bot_data = {"db": db, "scraper": registry, "http_client": MagicMock()}
    placeholder = MagicMock()
    placeholder.edit_text = AsyncMock()
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_user.language_code = "en"
    update.message.reply_text = AsyncMock(return_value=placeholder)
    response = SimpleNamespace(status_code=200, text=_PAGE)
    with patch("price_tracker.bot.handlers.debug.public_request", AsyncMock(return_value=response)):
        await cmd_debug(update, context)
    args: Any = placeholder.edit_text.await_args
    return str(args.args[0])


def _shown_name(report: str) -> str:
    assert "   Price: ❌ (e)" in report
    match = _NAME_LINE.search(report)
    assert match is not None, report
    return html.unescape(match.group(1))


@pytest.mark.asyncio
async def test_long_name_is_cut_to_sixty_cells_with_an_ellipsis() -> None:
    shown = _shown_name(await _debug_report("Kettle " * 20))
    assert shown.endswith("…")
    assert display_width(shown) <= 60


@pytest.mark.asyncio
async def test_wide_glyph_name_is_cut_by_cells_not_characters() -> None:
    shown = _shown_name(await _debug_report("電気ケトル" * 20))
    assert shown.endswith("…")
    assert display_width(shown) <= 60


@pytest.mark.asyncio
async def test_short_and_missing_names_are_unchanged() -> None:
    assert _shown_name(await _debug_report("Kettle")) == "Kettle"
    assert _shown_name(await _debug_report(None)) == "❌"


@pytest.mark.asyncio
async def test_hostile_name_never_breaks_the_markup() -> None:
    report = await _debug_report("<b>&" * 40)
    name_line = _NAME_LINE.search(report)
    assert name_line is not None
    assert "<b>" not in name_line.group(1)
    assert "&amp;" in name_line.group(1) or "&lt;" in name_line.group(1)
    assert not re.search(r"&(?!amp;|lt;|gt;)", name_line.group(1))
