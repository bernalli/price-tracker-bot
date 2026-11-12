"""CSV and command inputs must not poison a tracked product's money."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from price_tracker.bot.handlers.product import cmd_target
from price_tracker.bot.handlers.product_io import cmd_import
from price_tracker.core.scraper_base import ProductInfo
from tests.unit.test_product_io_csv_format import _context

URL = "https://example.com/item"


async def import_one(body: str, result: ProductInfo) -> tuple[MagicMock, AsyncMock]:
    update, context, db = _context(body)
    context.bot_data["scraper"].resolve.return_value.scrape.return_value = result
    with patch("price_tracker.core.url_utils.validate_public_url", return_value=None):
        await cmd_import(update, context)
    return update, db


@pytest.mark.asyncio
async def test_csv_rejects_explicit_currency_conflicting_with_quote():
    update, db = await import_one(
        f"URL,Valuta\n{URL},EUR\n", ProductInfo(name="Item", price=Decimal("100"), currency="GBP")
    )
    db.add_product.assert_not_awaited()
    assert "currency" in update.message.reply_text.return_value.edit_text.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_csv_sold_out_does_not_save_placeholder_and_is_reported_pending():
    update, db = await import_one(
        f"URL,Valuta\n{URL},EUR\n",
        ProductInfo(name="Item", price=Decimal("12"), currency="EUR", available=False),
    )
    assert db.add_product.await_args.kwargs["initial_price"] is None
    assert "awaiting" in update.message.reply_text.return_value.edit_text.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_csv_unpriced_result_is_reported_pending():
    update, db = await import_one(f"URL,Valuta\n{URL},EUR\n", ProductInfo(error="blocked"))
    assert db.add_product.await_args.kwargs["initial_price"] is None
    assert "awaiting" in update.message.reply_text.return_value.edit_text.await_args.args[0].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["NaN", "Infinity", "-5"])
async def test_csv_rejects_nonfinite_or_negative_target_before_insert(target):
    update, db = await import_one(
        f"URL,Target,Valuta\n{URL},{target},EUR\n",
        ProductInfo(name="Item", price=Decimal("12"), currency="EUR"),
    )
    db.add_product.assert_not_awaited()
    db.set_target_price.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["NaN", "Infinity", "-5"])
async def test_target_command_rejects_nonfinite_or_negative_value(target):
    update, context, db = _context("URL\n")
    context.args = ["1", target]
    with patch(
        "price_tracker.bot.handlers.product._get_user_product",
        new=AsyncMock(
            return_value={
                "id": 1,
                "user_id": 1,
                "name": "Item",
                "currency": "EUR",
                "current_price": "12",
            }
        ),
    ):
        await cmd_target(update, context)
    db.set_target_price.assert_not_awaited()
    assert update.message.reply_text.await_args is not None


async def test_csv_price_with_unknown_scraped_currency_waits_for_verification():
    update, db = await import_one(
        f"URL,Valuta\n{URL},EUR\n", ProductInfo(name="Item", price=Decimal("99"))
    )
    assert db.add_product.await_args.kwargs["initial_price"] is None
    assert "awaiting" in update.message.reply_text.return_value.edit_text.await_args.args[0].lower()


async def test_csv_matching_quote_currency_and_valid_target_are_preserved():
    _, db = await import_one(
        f"URL,Valuta,Target\n{URL},GBP,25.50\n",
        ProductInfo(name="Item", price=Decimal("100"), currency="GBP"),
    )
    assert db.add_product.await_args.kwargs["initial_price"] == Decimal("100")
    assert db.add_product.await_args.kwargs["currency"] == "GBP"
    db.set_target_price.assert_awaited_once_with(1, Decimal("25.50"))
