"""_add_product shows a friendly message (not a generic error) when a site blocks us.

Scrapers now raise BlockEvent on 403/429/CAPTCHA (bug #7). The /add path must
catch it and tell the user the site is blocking automated requests, rather than
letting it bubble to the global error handler.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.product import _add_product
from price_tracker.core.exceptions import CaptchaDetected, HTTPBlockStatus, WAFBlocked


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


async def _add_with_block(exc: Exception) -> str:
    """Run _add_product against a scraper raising ``exc``; return the shown text."""
    db = AsyncMock()
    db.get_product_by_url_for_user = AsyncMock(return_value=None)

    async def _raise(url: str, client: object) -> object:  # noqa: ARG001
        raise exc

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
    await _add_product(update, context, "http://93.184.216.34/p/1")
    db.add_product.assert_not_awaited()
    text: str = msg.edit_text.await_args.args[0]
    return text


@pytest.mark.parametrize(
    "exc",
    [
        HTTPBlockStatus(status=429, url="u"),
        CaptchaDetected(marker="g-recaptcha", url="u"),
    ],
)
async def test_transient_block_suggests_retrying(exc: Exception) -> None:
    text = await _add_with_block(exc)
    assert "try again in a few minutes" in text


@pytest.mark.parametrize(
    "exc",
    [
        HTTPBlockStatus(status=403, url="u"),
        WAFBlocked(provider="akamai", url="u"),
    ],
)
async def test_bot_protection_block_does_not_claim_permanence(exc: Exception) -> None:
    """The observed block cannot establish that every future request will be refused."""
    text = await _add_with_block(exc)
    assert "blocked this automated check" in text
    assert "permanent" not in text.lower()
    assert "try again in a few minutes" not in text.lower()
