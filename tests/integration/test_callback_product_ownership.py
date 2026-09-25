"""Legacy per-product callbacks act only on products the presser may see.

``callback_data`` is client-supplied: any user can send ``track_any_<id>`` or
``pref_*_<id>`` for an id they do not own. The buttons must change nothing on
someone else's product, must not reveal its name, and must not report success
for an id that does not exist. The owner (and an admin, who sees every product)
keep the normal behaviour.

The application is the production handler layout on a migrated in-memory
repository; updates go through ``Application.process_update`` against a fake
Telegram HTTP layer.
"""

from __future__ import annotations

import dataclasses
import itertools
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest

from price_tracker.bot.handlers import register_handlers
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from tests.support.fake_telegram import FakeRequest, callback_update, make_application

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from telegram.ext import Application

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "src" / "price_tracker" / "db" / "migrations"

ADMIN = 1
OWNER = 10
OTHER = 11
PRODUCT_NAME = "Kettle"
MISSING_ID = 999
NOT_FOUND = "❌ Prodotto non trovato."
PREF_PREFIXES = ["pref_new_", "pref_used_", "pref_amazon_", "pref_anyseller_", "pref_default_"]
PRODUCT_PREFIXES = ["track_any_", *PREF_PREFIXES]
_message_ids = itertools.count(1)


class Bot:
    """The production handler layout around a fake Telegram and a real repository."""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self.request = FakeRequest()
        self.app: Application[Any, Any, Any, Any, Any, Any] = make_application(
            self.request, with_job_queue=True
        )
        register_handlers(self.app)
        self.app.bot_data["db"] = repo
        self.errors: list[BaseException] = []
        self.app.add_error_handler(self._record_error)
        self.product = 0

    async def _record_error(self, update: object, context: Any) -> None:
        del update
        self.errors.append(context.error)

    async def press(self, user_id: int, data: str) -> None:
        update = callback_update(
            self.app.bot, user_id, user_id, data, message_id=next(_message_ids)
        )
        await self.app.update_processor.process_update(update, self.app.process_update(update))

    def replies(self) -> list[str]:
        return [
            str(c.params["text"])
            for c in self.request.calls
            if not c.failed and "text" in c.params and c.method != "answerCallbackQuery"
        ]

    async def row(self, product_id: int) -> dict[str, Any]:
        product = await self.repo.get_product(product_id)
        assert product is not None
        return dataclasses.asdict(product)


@pytest.fixture
async def bot() -> AsyncIterator[Bot]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(ADMIN, is_admin=True)
    await repo.ensure_user(OWNER)
    await repo.ensure_user(OTHER)
    wired = Bot(repo)
    wired.product = await repo.add_product(
        user_id=OWNER,
        url="https://shop.example/item/1",
        name=PRODUCT_NAME,
        domain="shop.example",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    await wired.app.initialize()
    try:
        yield wired
        assert wired.errors == []
    finally:
        await wired.app.shutdown()
        await conn.close()


@pytest.mark.parametrize("prefix", PRODUCT_PREFIXES)
async def test_non_owner_press_leaves_the_product_unchanged_and_hidden(
    bot: Bot, prefix: str
) -> None:
    before = await bot.row(bot.product)

    await bot.press(OTHER, f"{prefix}{bot.product}")

    assert await bot.row(bot.product) == before
    replies = bot.replies()
    assert replies == [NOT_FOUND]
    assert not any(PRODUCT_NAME in text for text in replies)


@pytest.mark.parametrize("prefix", PRODUCT_PREFIXES)
async def test_missing_product_press_reports_not_found(bot: Bot, prefix: str) -> None:
    before = await bot.row(bot.product)

    await bot.press(OWNER, f"{prefix}{MISSING_ID}")

    assert bot.replies() == [NOT_FOUND]
    assert await bot.row(bot.product) == before


async def test_owner_track_any_sets_any_drop(bot: Bot) -> None:
    await bot.press(OWNER, f"track_any_{bot.product}")

    row = await bot.row(bot.product)
    assert (row["threshold_type"], str(row["threshold_value"])) == ("any_drop", "0")
    assert PRODUCT_NAME in bot.replies()[-1]


async def test_owner_pref_new_sets_condition(bot: Bot) -> None:
    await bot.press(OWNER, f"pref_new_{bot.product}")

    row = await bot.row(bot.product)
    assert (row["preferred_condition"], row["preferred_seller"]) == ("new", None)
    assert PRODUCT_NAME in bot.replies()[-1]


async def test_admin_may_set_preferences_on_any_product(bot: Bot) -> None:
    await bot.press(ADMIN, f"pref_amazon_{bot.product}")
    await bot.press(ADMIN, f"track_any_{bot.product}")

    row = await bot.row(bot.product)
    assert (row["preferred_condition"], row["preferred_seller"]) == (None, "amazon")
    assert row["threshold_type"] == "any_drop"
