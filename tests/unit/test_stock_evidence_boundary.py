"""Stock evidence and monetary-unit boundary regressions."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.product import _add_product
from price_tracker.core.scraper_base import ProductInfo


def test_legacy_product_info_keeps_its_stock_semantics():
    assert ProductInfo().stock_evidence is True
    assert ProductInfo(available=False, stock_evidence=False).stock_evidence is False


async def test_price_without_currency_cannot_update_stored_eur(repo_with_product):  # noqa: F811
    from tests.unit.test_check_out_of_stock import _pull

    repo, pid = repo_with_product
    (result,) = await _pull(repo, pid, [ProductInfo(price=Decimal("10"), currency=None)])
    row = await repo.get_product(pid)
    assert result.reason == "currency_unknown"
    assert row.current_price == Decimal("100")
    assert await repo.get_price_history(pid) == []


async def test_unproven_false_is_not_an_out_of_stock_observation(repo_with_product):  # noqa: F811
    from tests.unit.test_check_out_of_stock import _pull

    repo, pid = repo_with_product
    (result,) = await _pull(
        repo,
        pid,
        [ProductInfo(price=Decimal("100"), currency="EUR", available=False, stock_evidence=False)],
    )
    row = await repo.get_product(pid)
    assert result.reason == "availability_unknown"
    assert row.is_available is True
    assert row.sold_out_streak == 0


from tests.unit.test_check_out_of_stock import repo_with_product  # noqa: E402,F401


async def test_add_rejects_scraped_price_without_currency():
    db = AsyncMock()
    db.get_product_by_url_for_user.return_value = None
    db.add_product.return_value = 1
    scraper = MagicMock()
    scraper.resolve.return_value.scrape = AsyncMock(
        return_value=ProductInfo(name="Item", price=Decimal("10"))
    )
    message = AsyncMock()
    update = MagicMock()
    update.effective_user.id = 1
    update.message.reply_text = AsyncMock(return_value=message)
    context = MagicMock()
    context.bot_data = {
        "db": db,
        "http_client": MagicMock(),
        "scraper": scraper,
        "config": MagicMock(),
    }
    await _add_product(update, context, "http://93.184.216.34/p/1")
    db.add_product.assert_not_awaited()


def test_availability_unknown_has_human_localized_explanation():
    import gettext
    from pathlib import Path

    import price_tracker
    from price_tracker.bot.ui.check_status import _REASONS

    msgid = _REASONS["availability_unknown"]
    translations = Path(price_tracker.__file__).parent / "locale"
    it = gettext.translation("messages", translations, languages=["it_IT"])
    en = gettext.translation("messages", translations, languages=["en"])
    assert it.gettext(msgid) == "Prezzo aggiornato, disponibilità non verificata."
    assert en.gettext(msgid) == "Price updated; availability could not be verified."


def test_sold_out_card_marks_unknown_read_as_last_confirmed():
    from dataclasses import replace

    from price_tracker.bot.ui.cards import product_card
    from tests.support.card_variants import NOW, VARIANTS, actions_for

    view = replace(VARIANTS["sold_out"], last_check_outcome="availability_unknown")
    screen = product_card(view, actions_for(view), now=NOW)
    assert "Last confirmed: sold out" in screen.text
    assert "Price updated; availability could not be verified." in screen.text


@pytest.mark.parametrize(
    "amount", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("1000000001")]
)
async def test_add_rejects_invalid_numeric_quote(amount):
    db = AsyncMock()
    db.get_product_by_url_for_user.return_value = None
    db.add_product.return_value = 1
    scraper = MagicMock()
    scraper.resolve.return_value.scrape = AsyncMock(
        return_value=ProductInfo(name="Item", price=amount, currency="EUR")
    )
    message = AsyncMock()
    update = MagicMock()
    update.effective_user.id = 1
    update.message.reply_text = AsyncMock(return_value=message)
    context = MagicMock()
    context.bot_data = {
        "db": db,
        "http_client": MagicMock(),
        "scraper": scraper,
        "config": MagicMock(),
    }
    await _add_product(update, context, "http://93.184.216.34/p/1")
    db.add_product.assert_not_awaited()


async def test_unknown_read_between_sold_out_reads_preserves_streak(repo_with_product):  # noqa: F811
    from tests.unit.test_check_out_of_stock import _pull

    repo, pid = repo_with_product
    results = await _pull(
        repo,
        pid,
        [
            ProductInfo(available=False),
            ProductInfo(price=Decimal("100"), currency="EUR", stock_evidence=False),
            ProductInfo(available=False),
        ],
    )
    row = await repo.get_product(pid)
    assert [r.reason for r in results] == ["out_of_stock", "availability_unknown", "out_of_stock"]
    assert row.is_available is False
    assert row.sold_out_streak == 2


async def test_add_rejects_price_returned_with_scrape_error():
    db = AsyncMock()
    db.get_product_by_url_for_user.return_value = None
    db.add_product.return_value = 1
    scraper = MagicMock()
    scraper.resolve.return_value.scrape = AsyncMock(
        return_value=ProductInfo(
            name="Item", price=Decimal("10"), currency="EUR", error="unreadable"
        )
    )
    message = AsyncMock()
    update = MagicMock()
    update.effective_user.id = 1
    update.message.reply_text = AsyncMock(return_value=message)
    context = MagicMock()
    context.bot_data = {
        "db": db,
        "http_client": MagicMock(),
        "scraper": scraper,
        "config": MagicMock(),
    }
    await _add_product(update, context, "http://93.184.216.34/p/1")
    db.add_product.assert_not_awaited()


async def test_scheduler_rejects_price_returned_with_scrape_error(repo_with_product):  # noqa: F811
    from tests.unit.test_check_out_of_stock import _pull

    repo, pid = repo_with_product
    (result,) = await _pull(
        repo, pid, [ProductInfo(price=Decimal("10"), currency="EUR", error="unreadable")]
    )
    row = await repo.get_product(pid)
    assert result.reason == "price_none"
    assert row.current_price == Decimal("100")
    assert await repo.get_price_history(pid) == []
