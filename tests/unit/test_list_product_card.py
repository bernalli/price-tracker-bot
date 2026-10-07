"""`/lista` is one paginated message; a product opens as the product card in place.

The card is localised (language of the user's Telegram app), its buttons trigger
the callbacks the legacy product actions already handle, and its back button
returns to the page and filter it was opened from.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio
from freezegun import freeze_time

from price_tracker.bot.handlers._cards import card_actions, product_view, screen_markup
from price_tracker.bot.handlers.callbacks import handle_callback
from price_tracker.bot.handlers.product_list import cmd_list
from price_tracker.bot.ui.cards import product_card
from price_tracker.config import Config
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")
USER_ID = 900000020


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repository = Repository(conn)
    await repository.ensure_user(USER_ID)
    try:
        yield repository
    finally:
        await conn.close()


def _config() -> Config:
    return Config(
        telegram_bot_token="x",
        admin_users=(),
        check_interval_minutes=360,
        database_path=":memory:",
        default_threshold_type="percentage",
        default_threshold_value="10",
        max_consecutive_errors=10,
        check_delay_seconds=0.0,
        notification_cooldown_hours=24,
        request_timeout=30,
        log_level="INFO",
        lang="it",
    )


def _update(language_code: str, query: MagicMock | None = None) -> MagicMock:
    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = USER_ID
    update.effective_user.language_code = language_code
    update.effective_user.first_name = "User"
    update.effective_user.full_name = "User"
    update.effective_user.username = None
    update.message.reply_text = AsyncMock()
    return update


async def _add_widget(repo: Repository, *, currency: str = "EUR", slug: str = "widget") -> int:
    return await repo.add_product(
        user_id=USER_ID,
        url=f"https://example.com/products/{slug}",
        name="Widget",
        domain="example.com",
        initial_price=Decimal("19.99"),
        currency=currency,
    )


def _query(data: str) -> MagicMock:
    query = MagicMock()
    query.data = data
    query.from_user.id = USER_ID
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def _wired_context(repo: Repository) -> MagicMock:
    context = MagicMock()
    context.bot_data = {"db": repo, "config": _config()}
    context.user_data = {}
    return context


@pytest.mark.parametrize(("language_code", "heading"), [("it", "Ora"), ("en", "Now")])
async def test_the_card_opened_in_place_is_the_localised_product_card(
    repo: Repository, language_code: str, heading: str
) -> None:
    pid = await _add_widget(repo)
    query = _query(f"l:e:2:{pid}")

    with freeze_time("2026-03-01 12:00:00", real_asyncio=True):
        await handle_callback(_update(language_code, query), _wired_context(repo))
        record = await repo.get_product(pid)
        assert record is not None
        view = product_view(record, default_interval_minutes=360)
        expected = product_card(view, card_actions(view, back="l:e:2"), now=datetime.now(UTC))

    call = query.edit_message_text.await_args
    assert call.args[0] == expected.text
    assert call.kwargs["reply_markup"] == screen_markup(expected)
    assert heading in call.args[0]
    assert "Widget" in call.args[0]
    callbacks = {
        b.callback_data
        for row in call.kwargs["reply_markup"].inline_keyboard
        for b in row
        if b.callback_data
    }
    assert callbacks == {
        f"check_{pid}",
        f"chart_{pid}",
        f"pause_{pid}",
        f"remove_{pid}",
        f"edit_{pid}",
        f"setrefresh_{pid}",
        "l:e:2",
    }


async def test_lista_sends_one_message_with_the_page(repo: Repository) -> None:
    ids = [await _add_widget(repo, slug=f"widget-{n}") for n in range(7)]
    update = _update("en")
    context = MagicMock()
    context.bot_data = {"db": repo, "config": _config()}

    await cmd_list(update, context)

    update.message.reply_text.assert_awaited_once()
    call = update.message.reply_text.await_args
    assert call.kwargs["parse_mode"] == "HTML"
    assert "page 1/2" in call.args[0]
    callbacks = [
        b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for b in row
    ]
    assert callbacks[:5] == [f"l:a:1:{pid}" for pid in ids[:5]]
    assert "delete_all" in callbacks


async def test_lista_without_products_keeps_the_old_text_and_no_keyboard(repo: Repository) -> None:
    update = _update("it")
    context = MagicMock()
    context.bot_data = {"db": repo, "config": _config()}

    await cmd_list(update, context)

    update.message.reply_text.assert_awaited_once_with(
        "📭 Non hai prodotti tracciati.\nIncollami un link per iniziare!"
    )


async def test_lista_with_only_paused_products_shows_the_empty_active_page(
    repo: Repository,
) -> None:
    pid = await _add_widget(repo)
    await repo.pause_product(pid)
    update = _update("en")
    context = MagicMock()
    context.bot_data = {"db": repo, "config": _config()}

    await cmd_list(update, context)

    call = update.message.reply_text.await_args
    assert call.args[0].endswith("Nothing here.")
    callbacks = [
        b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for b in row
    ]
    assert "l:p:1" in callbacks


async def test_product_view_maps_a_stored_record(repo: Repository) -> None:
    pid = await _add_widget(repo, currency="CHF")
    await repo.set_check_interval(pid, 90)
    record = await repo.get_product(pid)
    assert record is not None

    view = product_view(record, default_interval_minutes=360)

    assert view.id == pid
    assert view.status == "active"
    assert view.currency == "CHF"
    assert view.current == Decimal("19.99")
    assert view.check_interval_minutes == 90
    assert view.reference_currency == "EUR"
    assert view.reference_estimate is not None


async def test_product_view_drops_out_of_range_stored_values(repo: Repository) -> None:
    pid = await _add_widget(repo, currency="XYZ")
    await repo._conn.execute(
        "UPDATE products SET current_price = '-1', lowest_price = 'NaN' WHERE id = ?", (pid,)
    )
    await repo._conn.commit()
    record = await repo.get_product(pid)
    assert record is not None

    view = product_view(record, default_interval_minutes=360)

    assert view.currency == "EUR"
    assert view.current is None
    assert view.lowest is None
