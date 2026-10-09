"""Every product list uses one bounded, escaped row per product."""

from __future__ import annotations

import inspect
import json
import re
from datetime import UTC, datetime
from html import escape, unescape
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.bot.callbacks import Action
from price_tracker.bot.handlers import history, monitoring, product
from price_tracker.bot.handlers._cards import list_view
from price_tracker.bot.handlers.callbacks import _ops
from price_tracker.bot.handlers.callbacks._menu import handle_menu_navigation
from price_tracker.bot.handlers.monitoring import cmd_checkall
from price_tracker.bot.messages import reset_locale, set_locale
from price_tracker.bot.ui.cards import list_page
from price_tracker.bot.ui.product_rows import product_row
from price_tracker.bot.ui.width import display_width
from price_tracker.core.textlimits import _is_valid_telegram_markup, paginate
from price_tracker.db.models import DigestEntry
from price_tracker.notifier.digest import _digest_blocks
from tests.support.list_variants import record

if TYPE_CHECKING:
    from decimal import Decimal


def plain(text: str) -> str:
    return unescape(re.sub(r"<[^>]*>", "", text))


def assert_rows(rows: list[str], count: int) -> None:
    assert len(rows) == count
    for index, row in enumerate(rows, 1):
        assert _is_valid_telegram_markup(row)
        shown = plain(row)
        assert len(shown.splitlines()) == 1
        assert display_width(shown) <= 32
        assert f"P{index}:" in shown
        assert "90" in shown
        assert "▼" in shown
        assert "10" in shown
        assert "%" in shown
    assert all(sum(f"P{i}:" in plain(row) for row in rows) == 1 for i in range(1, count + 1))


@pytest.mark.parametrize("locale", ["en", "it", "de", "fr", "es", "uk", "pt_BR", "ja", "zh_Hans"])
@settings(max_examples=12, deadline=None)
@given(
    tail=st.sampled_from(["Long Latin <&> name " * 12, "商品名称宽字符" * 20, "📦😀🎧" * 30]),
    count=st.integers(min_value=1, max_value=8),
)
async def test_every_list_has_one_narrow_row_per_product(
    tail: str, count: int, locale: str
) -> None:
    token = set_locale(locale)
    try:
        products = [
            record(i, name=f"P{i}:{tail}\ncontinued", current_price="90", initial_price="100")
            for i in range(1, count + 1)
        ]
        rows: list[str] = []
        for page in range(1, (count + 4) // 5 + 1):
            screen = list_page(list_view(products, "a", page, default_interval_minutes=60))
            rows.extend(screen.text.split("\n\n", 1)[1].splitlines())
        assert_rows(rows, count)

        db = MagicMock()
        db.get_active_products = AsyncMock(return_value=products)
        db.get_all_products = AsyncMock(return_value=[dict(p, is_active=0) for p in products])
        context = MagicMock()
        context.bot_data = {
            "db": db,
            "scheduler": MagicMock(check_user_products_for_user=AsyncMock(return_value=[])),
        }
        for menu in ("products", "paused", "prices", "history", "notifications", "check_all"):
            query = MagicMock(edit_message_text=AsyncMock())
            await handle_menu_navigation(query, context, db, 1, Action(menu))
            call = query.edit_message_text.call_args
            if menu == "check_all":
                assert_rows(call.args[0].split("\n\n", 1)[1].splitlines(), count)
            else:
                labels = [
                    button.text
                    for row in call.kwargs["reply_markup"].inline_keyboard
                    for button in row
                    if button.callback_data.startswith("p:")
                ]
                # Buttons are literal Telegram text, not HTML.
                assert_rows(
                    [escape(label) for label in labels],
                    count,
                )

        update = MagicMock()
        update.message.reply_text = AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
        update.effective_user.id = 1
        await inspect.unwrap(cmd_checkall)(update, context)
        text = update.message.reply_text.return_value.edit_text.call_args.args[0]
        assert_rows(text.split("\n\n", 1)[1].splitlines(), count)

        context.args = []
        for command in (history.cmd_history, product.cmd_delete, monitoring.cmd_reactivate):
            update.message.reply_text.reset_mock()
            await inspect.unwrap(command)(update, context)
            markup = update.message.reply_text.call_args.kwargs["reply_markup"]
            assert_rows(
                [
                    escape(button.text)
                    for row in markup.inline_keyboard
                    for button in row
                    if button.callback_data.startswith("p:")
                ],
                count,
            )
        for picker, action in (
            (product._product_picker, "settarget"),
            (product._product_picker, "setsoglia"),
            (monitoring._product_picker, "check"),
            (monitoring._product_picker, "pause"),
            (monitoring._product_picker, "setrefresh"),
        ):
            update.message.reply_text.reset_mock()
            await picker(update, context, action, "Choose")
            markup = update.message.reply_text.call_args.kwargs["reply_markup"]
            assert_rows(
                [escape(button.text) for row in markup.inline_keyboard for button in row], count
            )

        entries = [
            DigestEntry(
                id=p["id"],
                user_id=1,
                product_id=p["id"],
                enqueued_at=datetime.now(UTC),
                alert_payload_json=json.dumps(
                    {
                        "product_name": p["name"],
                        "new_price": "90",
                        "old_price": "100",
                        "currency": "EUR",
                    }
                ),
            )
            for p in products
        ]
        header, blocks, footer, rejected = _digest_blocks(entries)
        assert not rejected
        pages = paginate(header, blocks, footer, limit=240)
        assert [key for _, keys in pages for key in keys] == list(range(1, count + 1))
        assert all(_is_valid_telegram_markup(text) for text, _ in pages)
        assert_rows([row for _, row in blocks], count)
    finally:
        reset_locale(token)


@pytest.mark.parametrize("locale", ["en", "it", "ja", "zh_Hans", "de"])
@given(
    current=st.decimals(min_value=0, max_value="999999999999999.99", places=2),
    initial=st.decimals(min_value="0.01", max_value="999999999999999.99", places=2),
    currency=st.sampled_from(["EUR", "USD", "JPY", "CHF"]),
    name=st.text(alphabet="商品📦😀<&>Latin\n\r\t", min_size=60, max_size=150),
)
def test_list_width_with_extreme_prices(
    current: Decimal, initial: Decimal, currency: str, name: str, locale: str
) -> None:
    token = set_locale(locale)
    try:
        row = product_row(name, current, initial=initial, currency=currency, mark="⚠️")
        assert _is_valid_telegram_markup(row)
        assert len(plain(row).splitlines()) == 1
        assert display_width(plain(row)) <= 32
        assert "<b>" in row
        if current != initial:
            assert ("▼" if current < initial else "▲") in row
            assert "%" in row
    finally:
        reset_locale(token)


@given(tail=st.sampled_from(["Long <&> name " * 12, "商品名称" * 30, "📦😀" * 30]))
async def test_operational_product_lists_are_narrow(tail: str) -> None:
    from price_tracker.bot.handlers.debug import errors_text
    from price_tracker.core.alert import format_operational_notice, format_warning_notice
    from price_tracker.core.notices import NoticeGroup, OperationalEvent
    from price_tracker.db.models import ProductErrorRow

    token = set_locale("en")
    try:
        events = tuple(
            OperationalEvent(
                event="suspended",
                user_id=1,
                product_id=i,
                product_name=f"P{i}:{tail}",
                url=f"https://shop.example/{i}",
                group_key="shop.example",
                reason="parse_error",
                detail=None,
                last_error="parse_error",
                error_count=3,
                max_errors=3,
                last_price=None,
                currency="EUR",
                last_checked_at=None,
            )
            for i in range(1, 4)
        )
        for kind, render in (
            ("suspended", format_operational_notice),
            ("warning", format_warning_notice),
        ):
            text = render(
                NoticeGroup(
                    "suspended" if kind == "suspended" else "warning", 1, "shop.example", events
                )
            )
            rows = [line for line in text.splitlines() if "P" in line and ":" in line]
            assert len(rows) == 3
            for i, row in enumerate(rows, 1):
                assert f"P{i}:" in row
                assert display_width(plain(row)) <= 32
                assert _is_valid_telegram_markup(row)
            assert sum("<b>N/A</b>" in line for line in text.splitlines()) == 3
        db = MagicMock(
            list_products_with_errors=AsyncMock(
                return_value=[
                    ProductErrorRow(
                        i,
                        f"P{i}:{tail}",
                        f"https://shop.example/{i}",
                        "shop.example",
                        3,
                        "parse_error",
                        None,
                    )
                    for i in range(1, 4)
                ]
            )
        )
        error_report = await errors_text(db, None, 1)
        assert error_report is not None
        rows = error_report.split("\n\n", 1)[1].split("\n\n", 1)[0].splitlines()
        assert len(rows) == 3
        assert all(display_width(plain(row)) <= 32 for row in rows)
        assert all(_is_valid_telegram_markup(row) for row in rows)
        context = MagicMock()
        context.bot_data = {
            "scheduler": MagicMock(
                check_products_for_user=AsyncMock(return_value=[MagicMock(reason=None)] * 3)
            )
        }
        db.reactivate_product = AsyncMock()
        db.get_product = AsyncMock(
            side_effect=lambda i: record(
                i, name=f"P{i}:{tail}", current_price="90", initial_price="100"
            )
        )
        query = MagicMock(edit_message_text=AsyncMock())
        with patch.object(
            _ops,
            "_load_group",
            AsyncMock(return_value=("shop.example", [MagicMock(id=i) for i in range(1, 4)])),
        ):
            await _ops._handle_reactivate(query, context, db, 1, Action("ops.reactivate", (1,)))
        assert_rows(query.edit_message_text.call_args.args[0].splitlines()[1:], 3)
    finally:
        reset_locale(token)


async def test_check_lists_paginate_without_losing_products() -> None:
    token = set_locale("en")
    try:
        products = [
            record(i, name=f"P{i}:" + "商品<&>" * 30, current_price="90", initial_price="100")
            for i in range(1, 201)
        ]
        db = MagicMock(get_active_products=AsyncMock(return_value=products))
        context = MagicMock()
        context.bot_data = {
            "db": db,
            "scheduler": MagicMock(check_user_products_for_user=AsyncMock(return_value=[])),
        }
        query = MagicMock(edit_message_text=AsyncMock())
        query.message.reply_text = AsyncMock()
        await handle_menu_navigation(query, context, db, 1, Action("check_all"))
        pages = [
            query.edit_message_text.call_args.args[0],
            *(call.args[0] for call in query.message.reply_text.call_args_list),
        ]
        assert len(pages) > 1
        from price_tracker.core.textlimits import visible_length

        assert all(visible_length(page) <= 4000 for page in pages)
        assert_rows(
            [line for page in pages for line in page.splitlines() if "<b>€90.00</b>" in line], 200
        )
        assert all(_is_valid_telegram_markup(page) for page in pages)
    finally:
        reset_locale(token)
