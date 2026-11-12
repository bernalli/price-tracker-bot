"""User-facing status must reflect whether a fresh price was accepted."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from price_tracker.bot.callbacks import Action
from price_tracker.bot.handlers import monitoring
from price_tracker.bot.handlers.callbacks import _ops
from price_tracker.bot.handlers.debug import errors_text
from price_tracker.bot.messages import set_locale
from price_tracker.core.scheduler import CheckResult
from price_tracker.db.models import ProductErrorRow
from tests.unit.test_check_out_of_stock import _check_env


@pytest.mark.parametrize(
    "reason", ["block", "price_none", "quarantined", "awaiting_confirmation", "currency_mismatch"]
)
async def test_check_command_does_not_claim_success_for_unaccepted_read(reason):
    update, context, placeholder = _check_env(reason)
    await monitoring.cmd_check(update, context)
    text = placeholder.edit_text.await_args.args[0]
    assert "No significant change" not in text
    assert not text.startswith("✅")


async def test_check_command_formats_the_product_currency():
    update, context, placeholder = _check_env(None)
    context.bot_data["db"].get_product.return_value["currency"] = "USD"
    await monitoring.cmd_check(update, context)
    text = placeholder.edit_text.await_args.args[0]
    assert "$50.00" in text
    assert "€50.00" not in text


async def test_group_results_follow_product_id_not_result_position():
    set_locale("en")
    group = [
        SimpleNamespace(
            id=1,
            get=lambda k, default=None: {"name": "First", "url": "https://shop.example/1"}.get(
                k, default
            ),
        ),
        SimpleNamespace(
            id=2,
            get=lambda k, default=None: {"name": "Second", "url": "https://shop.example/2"}.get(
                k, default
            ),
        ),
    ]
    products = {
        1: {
            "name": "First",
            "url": "https://shop.example/1",
            "current_price": "10",
            "initial_price": "10",
            "currency": "EUR",
        },
        2: {
            "name": "Second",
            "url": "https://shop.example/2",
            "current_price": "20",
            "initial_price": "20",
            "currency": "EUR",
        },
    }
    db = MagicMock(
        reactivate_product=AsyncMock(), get_product=AsyncMock(side_effect=lambda i: products[i])
    )
    scheduler = MagicMock(check_products_for_user=AsyncMock(return_value=[CheckResult(2, 7)]))
    context = MagicMock(bot_data={"scheduler": scheduler})
    query = MagicMock(edit_message_text=AsyncMock())
    with patch.object(_ops, "_load_group", AsyncMock(return_value=("shop.example", group))):
        await _ops._handle_reactivate(query, context, db, 7, Action("ops.reactivate", (1,)))
    lines = query.edit_message_text.await_args.args[0].splitlines()
    first = next(line for line in lines if "First" in line)
    second = next(line for line in lines if "Second" in line)
    assert "✅" not in first
    assert "✅" in second


async def test_errors_explains_reason_without_exposing_diagnostic_detail():
    set_locale("en")
    db = MagicMock(
        list_products_with_errors=AsyncMock(
            return_value=[
                ProductErrorRow(
                    3,
                    "Kettle",
                    "https://shop.example/1",
                    "shop.example",
                    2,
                    "parse_error: SECRET debug path",
                    None,
                )
            ]
        )
    )
    text = await errors_text(db, None, 7)
    assert text is not None
    assert "#3" in text
    assert "price" in text.lower()
    assert "SECRET" not in text


async def test_add_does_not_show_scraper_diagnostic(monkeypatch):
    from price_tracker.bot.handlers.product import _add_product
    from price_tracker.core.scraper_base import ProductInfo

    set_locale("en")
    monkeypatch.setattr("price_tracker.core.url_utils.validate_public_url", lambda url: None)
    scraper = MagicMock(
        scrape=AsyncMock(
            return_value=ProductInfo(name="Kettle", price=None, error="SECRET INTERNAL TRACE")
        )
    )
    db = MagicMock(get_product_by_url_for_user=AsyncMock(return_value=None))
    context = MagicMock(
        bot_data={
            "db": db,
            "http_client": object(),
            "scraper": MagicMock(resolve=MagicMock(return_value=scraper)),
        }
    )
    progress = MagicMock(edit_text=AsyncMock())
    update = MagicMock()
    update.effective_user.id = 7
    update.message.reply_text = AsyncMock(return_value=progress)
    await _add_product(update, context, "https://shop.example/item")
    text = progress.edit_text.await_args.args[0]
    assert "SECRET" not in text
    assert "price" in text.lower()


async def test_csv_decode_error_does_not_echo_raw_exception():
    from price_tracker.bot.handlers.product_io import cmd_import
    from tests.unit.test_product_io_csv_format import _context

    update, context, _ = _context("")

    async def invalid(buf):
        buf.write(b"\xff")

    context.bot.get_file.return_value.download_to_memory = AsyncMock(side_effect=invalid)
    await cmd_import(update, context)
    text = update.message.reply_text.await_args.args[0]
    assert "codec" not in text
    assert "CSV" in text


async def test_card_uses_last_recorded_price_when_latest_read_failed():
    from price_tracker.bot.handlers._cards import product_view
    from price_tracker.bot.ui.cards import _now_line

    set_locale("en")
    row = {
        "id": 1,
        "name": "Kettle",
        "url": "https://shop.example/1",
        "is_active": 1,
        "current_price": "10",
        "currency": "EUR",
        "consecutive_errors": 1,
    }
    text = _now_line(product_view(row, default_interval_minutes=60), loc="en")
    assert "Last recorded" in text
    assert "Now:" not in text


@pytest.mark.parametrize("reason", ["awaiting_confirmation", "quarantined", "currency_mismatch"])
async def test_check_button_explains_specific_non_success(reason):
    from price_tracker.bot.handlers.callbacks import _product

    update, context, _ = _check_env(reason)
    query = MagicMock(edit_message_text=AsyncMock())
    await _product.handle_check_button(query, context, context.bot_data["db"], 7, "check_1")
    text = query.edit_message_text.await_args.args[0]
    assert "no significant change" not in text.lower()
    assert "check failed" not in text.lower()


async def test_checkall_reports_skips_and_keeps_suspended_product():
    from price_tracker.bot.handlers.callbacks._menu import _handle_menu_checkall

    set_locale("en")
    original = [
        {
            "id": 1,
            "name": "First",
            "current_price": "10",
            "initial_price": "10",
            "currency": "EUR",
            "is_active": 1,
        },
        {
            "id": 2,
            "name": "Second",
            "current_price": "20",
            "initial_price": "20",
            "currency": "EUR",
            "is_active": 1,
        },
    ]
    db = MagicMock(get_active_products=AsyncMock(side_effect=[original, [original[0]]]))
    results = [
        CheckResult(1, 7, reason="quarantined"),
        CheckResult(2, 7, disabled=True, reason="price_none"),
    ]
    scheduler = MagicMock(check_user_products_for_user=AsyncMock(return_value=results))
    context = MagicMock(bot_data={"scheduler": scheduler})
    query = MagicMock(edit_message_text=AsyncMock())
    await _handle_menu_checkall(query, context, db, 7)
    text = query.edit_message_text.await_args.args[0]
    assert "First" in text
    assert "Second" in text
    assert "postponed" in text.lower()
    assert "price" in text.lower()
    assert not text.startswith("✅")


async def test_ops_group_only_affects_same_suspension_reason():
    set_locale("en")
    anchor = {
        "id": 1,
        "user_id": 7,
        "url": "https://shop.example/1",
        "suspension_reason": "listing_gone",
        "suspension_kind": "automatic",
    }
    group = [
        SimpleNamespace(id=1, get=lambda k, default=None: anchor.get(k, default)),
        SimpleNamespace(
            id=2,
            get=lambda k, default=None: {
                "url": "https://shop.example/2",
                "suspension_reason": "parse_error",
            }.get(k, default),
        ),
    ]
    db = MagicMock(list_auto_suspended_products=AsyncMock(return_value=group))
    with patch.object(_ops, "_get_user_product", AsyncMock(return_value=anchor)):
        _, _, selected = await _ops._automatic_group(MagicMock(), db, 7, 1)
    assert [item.id for item in selected] == [1]


async def test_add_distinguishes_explicitly_unavailable_product(monkeypatch):
    from price_tracker.bot.handlers.product import _add_product
    from price_tracker.core.scraper_base import ProductInfo

    set_locale("en")
    monkeypatch.setattr("price_tracker.core.url_utils.validate_public_url", lambda url: None)
    scraper = MagicMock(
        scrape=AsyncMock(return_value=ProductInfo(name="Kettle", price=None, available=False))
    )
    db = MagicMock(get_product_by_url_for_user=AsyncMock(return_value=None))
    context = MagicMock(
        bot_data={
            "db": db,
            "http_client": object(),
            "scraper": MagicMock(resolve=MagicMock(return_value=scraper)),
        }
    )
    progress = MagicMock(edit_text=AsyncMock())
    update = MagicMock()
    update.effective_user.id = 7
    update.message.reply_text = AsyncMock(return_value=progress)
    await _add_product(update, context, "https://shop.example/item")
    assert "unavailable" in progress.edit_text.await_args.args[0].lower()


async def test_add_missing_page_does_not_claim_removed(monkeypatch):
    from price_tracker.bot.handlers.product import _add_product
    from price_tracker.core.exceptions import ListingGone

    set_locale("en")
    monkeypatch.setattr("price_tracker.core.url_utils.validate_public_url", lambda url: None)
    url = "https://shop.example/item"
    scraper = MagicMock(scrape=AsyncMock(side_effect=ListingGone(status=410, url=url)))
    db = MagicMock(get_product_by_url_for_user=AsyncMock(return_value=None))
    context = MagicMock(
        bot_data={
            "db": db,
            "http_client": object(),
            "scraper": MagicMock(resolve=MagicMock(return_value=scraper)),
        }
    )
    progress = MagicMock(edit_text=AsyncMock())
    update = MagicMock()
    update.effective_user.id = 7
    update.message.reply_text = AsyncMock(return_value=progress)
    await _add_product(update, context, url)
    text = progress.edit_text.await_args.args[0].lower()
    assert "page" in text
    assert "link" in text
    assert "removed" not in text


async def test_add_ambiguous_currency_explains_why_price_was_not_accepted(monkeypatch):
    from price_tracker.bot.handlers.product import _add_product
    from price_tracker.core.scraper_base import ProductInfo

    set_locale("en")
    monkeypatch.setattr("price_tracker.core.url_utils.validate_public_url", lambda url: None)
    scraper = MagicMock(
        scrape=AsyncMock(
            return_value=ProductInfo(name="Kettle", price=None, error="currency_ambiguous")
        )
    )
    db = MagicMock(get_product_by_url_for_user=AsyncMock(return_value=None))
    context = MagicMock(
        bot_data={
            "db": db,
            "http_client": object(),
            "scraper": MagicMock(resolve=MagicMock(return_value=scraper)),
        }
    )
    progress = MagicMock(edit_text=AsyncMock())
    update = MagicMock()
    update.effective_user.id = 7
    update.message.reply_text = AsyncMock(return_value=progress)
    await _add_product(update, context, "https://shop.example/item")
    text = progress.edit_text.await_args.args[0].lower()
    assert "currency" in text
    assert "currency_ambiguous" not in text


async def test_sold_out_check_does_not_present_stale_price_as_current():
    update, context, placeholder = _check_env("out_of_stock")
    await monitoring.cmd_check(update, context)
    text = placeholder.edit_text.await_args.args[0]
    assert "Last recorded price:" in text


async def test_check_command_handles_missing_result_as_incomplete():
    update, context, placeholder = _check_env(None)
    context.bot_data["scheduler"].check_one_product_for_user.return_value = None
    await monitoring.cmd_check(update, context)
    text = placeholder.edit_text.await_args.args[0]
    assert "could not be completed" in text.lower()
    assert "No significant change" not in text


def test_compiled_catalog_translates_status_and_falls_back_to_english():
    from price_tracker.bot.handlers._check_status import check_reason
    from price_tracker.bot.messages import get_translation, reset_locale

    get_translation.cache_clear()
    token = set_locale("it_IT")
    try:
        assert (
            check_reason("currency_unknown")
            == "Non sono riuscito a determinare la valuta del prezzo."
        )
        assert get_translation("it_IT").gettext("Product pages unavailable on {domain}") == (
            "Pagine dei prodotti non disponibili su {domain}"
        )
    finally:
        reset_locale(token)
    token = set_locale("en")
    try:
        assert check_reason("currency_unknown") == "The price currency could not be determined."
    finally:
        reset_locale(token)


def test_http_error_does_not_assert_site_was_unreachable():
    from price_tracker.bot.handlers._check_status import check_reason

    set_locale("en")
    assert "could not be reached" not in check_reason("http_error")


async def test_group_recheck_reports_when_product_was_suspended_again():
    set_locale("en")
    group = [
        SimpleNamespace(
            id=1,
            get=lambda k, default=None: {"name": "First", "url": "https://shop.example/1"}.get(
                k, default
            ),
        )
    ]
    product = {
        "name": "First",
        "url": "https://shop.example/1",
        "current_price": "10",
        "initial_price": "10",
        "currency": "EUR",
    }
    db = MagicMock(reactivate_product=AsyncMock(), get_product=AsyncMock(return_value=product))
    scheduler = MagicMock(
        check_products_for_user=AsyncMock(
            return_value=[CheckResult(1, 7, disabled=True, reason="price_none")]
        )
    )
    query = MagicMock(edit_message_text=AsyncMock())
    with patch.object(_ops, "_load_group", AsyncMock(return_value=("shop.example", group))):
        await _ops._handle_reactivate(
            query,
            MagicMock(bot_data={"scheduler": scheduler}),
            db,
            7,
            Action("ops.reactivate", (1,)),
        )
    assert "suspended" in query.edit_message_text.await_args.args[0].lower()
