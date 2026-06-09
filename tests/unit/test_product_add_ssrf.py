"""Wiring guard: _add_product rejects SSRF URLs before storing/scraping (bug #4)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from price_tracker.bot.handlers.product import _add_product


async def test_add_product_rejects_loopback_url_before_scrape() -> None:
    db = AsyncMock()
    scraper = MagicMock()
    scraper.resolve = MagicMock()
    update = MagicMock()
    update.effective_user.id = 1
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot_data = {
        "db": db,
        "http_client": MagicMock(),
        "scraper": scraper,
        "config": MagicMock(),
    }

    await _add_product(update, context, "http://127.0.0.1:9090/metrics")

    # Rejected before the duplicate lookup and before any scraper resolution/fetch.
    db.get_product_by_url_for_user.assert_not_awaited()
    scraper.resolve.assert_not_called()
    update.message.reply_text.assert_awaited()
