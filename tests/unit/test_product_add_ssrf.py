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


PRIVATE_MESSAGE = "❌ URL not allowed: it points to a private or internal address."
SCHEME_MESSAGE = "❌ URL not allowed: only http:// and https:// links are supported."


def _wired() -> tuple[AsyncMock, MagicMock, MagicMock, MagicMock]:
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
    return db, scraper, update, context


async def test_add_product_rejects_non_http_scheme_with_its_own_message() -> None:
    db, scraper, update, context = _wired()

    await _add_product(update, context, "ftp://shop.example.com/x")

    update.message.reply_text.assert_awaited_once_with(SCHEME_MESSAGE)
    db.get_product_by_url_for_user.assert_not_awaited()
    db.add_product.assert_not_awaited()
    scraper.resolve.assert_not_called()


async def test_add_product_private_address_keeps_the_private_message() -> None:
    db, scraper, update, context = _wired()

    await _add_product(update, context, "http://169.254.169.254/x")

    update.message.reply_text.assert_awaited_once_with(PRIVATE_MESSAGE)
    db.get_product_by_url_for_user.assert_not_awaited()
    db.add_product.assert_not_awaited()
    scraper.resolve.assert_not_called()
