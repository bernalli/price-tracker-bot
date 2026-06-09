"""_add_product shows a friendly message (not a generic error) when a site blocks us.

Scrapers now raise BlockEvent on 403/429/CAPTCHA (bug #7). The /add path must
catch it and tell the user the site is blocking automated requests, rather than
letting it bubble to the global error handler.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from price_tracker.bot.handlers.product import _add_product
from price_tracker.core.exceptions import HTTPBlockStatus


async def test_add_product_handles_block_event_gracefully() -> None:
    db = AsyncMock()
    db.get_product_by_url_for_user = AsyncMock(return_value=None)

    async def _raise(url: str, client: object) -> object:  # noqa: ARG001
        raise HTTPBlockStatus(status=429, url=url)

    blocking = MagicMock()
    blocking.scrape = _raise
    scraper = MagicMock()
    scraper.resolve = MagicMock(return_value=blocking)

    msg = AsyncMock()
    update = MagicMock()
    update.effective_user.id = 1
    update.message.reply_text = AsyncMock(return_value=msg)
    context = MagicMock()
    context.bot_data = {
        "db": db,
        "http_client": MagicMock(),
        "scraper": scraper,
        "config": MagicMock(),
    }

    # Public IP literal → passes the SSRF check without a DNS lookup.
    await _add_product(update, context, "http://93.184.216.34/p/1")

    db.add_product.assert_not_awaited()  # product not stored
    msg.edit_text.assert_awaited()  # user got a friendly message
