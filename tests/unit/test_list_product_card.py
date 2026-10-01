"""`/lista` shows each product as the product card from `bot.ui`.

The card is localised (language of the user's Telegram app), and its buttons
trigger the callbacks the legacy product actions already handle.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio

from price_tracker.bot.handlers._cards import product_view
from price_tracker.bot.handlers.product_list import cmd_list
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


def _update(language_code: str) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = USER_ID
    update.effective_user.language_code = language_code
    update.effective_user.first_name = "User"
    update.effective_user.full_name = "User"
    update.effective_user.username = None
    update.message.reply_text = AsyncMock()
    return update


async def _add_widget(repo: Repository, *, currency: str = "EUR") -> int:
    return await repo.add_product(
        user_id=USER_ID,
        url="https://example.com/products/widget",
        name="Widget",
        domain="example.com",
        initial_price=Decimal("19.99"),
        currency=currency,
    )


def _card_calls(update: MagicMock) -> list[tuple[str, Any]]:
    calls = []
    for call in update.message.reply_text.await_args_list:
        markup = call.kwargs.get("reply_markup")
        if markup is not None and any(
            getattr(b, "callback_data", None) == "menu_prodotti"
            for row in markup.inline_keyboard
            for b in row
        ):
            calls.append((str(call.args[0]), markup.inline_keyboard))
    return calls


@pytest.mark.parametrize(("language_code", "heading"), [("it", "Ora"), ("en", "Now")])
async def test_lista_sends_one_localised_card_per_product(
    repo: Repository, language_code: str, heading: str
) -> None:
    pid = await _add_widget(repo)
    update = _update(language_code)
    context = MagicMock()
    context.bot_data = {"db": repo, "config": _config()}

    await cmd_list(update, context)

    cards = _card_calls(update)
    assert len(cards) == 1
    text, keyboard = cards[0]
    assert "Widget" in text
    assert f"#{pid}" in text
    assert heading in text
    callbacks = {b.callback_data for row in keyboard for b in row if b.callback_data}
    assert callbacks == {
        f"check_{pid}",
        f"chart_{pid}",
        f"pause_{pid}",
        f"remove_{pid}",
        f"edit_{pid}",
        f"setrefresh_{pid}",
        "menu_prodotti",
    }
    urls = [b.url for row in keyboard for b in row if b.url]
    assert urls == ["https://example.com/products/widget"]


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
