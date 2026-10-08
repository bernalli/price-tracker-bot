"""The card actions redraw the card (or the list) in place, with a one-line result on top.

Real in-memory repository, mocked Telegram query, fake scheduler.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio
from telegram.error import BadRequest

import price_tracker
from price_tracker.bot.callbacks import InvalidCallback, decode
from price_tracker.bot.handlers._cards import (
    card_actions,
    empty_list_text,
    list_view,
    product_view,
    screen_markup,
)
from price_tracker.bot.handlers.callbacks import handle_callback
from price_tracker.bot.messages import set_locale
from price_tracker.bot.ui.cards import list_page, product_card
from price_tracker.core.alert import PriceAlert
from price_tracker.core.scheduler import CheckResult
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from price_tracker.app.views import ListFilter
    from price_tracker.bot.ui.screens import Screen

MIGRATIONS_DIR = Path(price_tracker.__file__).resolve().parent / "db" / "migrations"
USER = 10
OTHER = 12
ADMIN = 13
BAD_ID = "❌ ID non valido."
NOT_FOUND = "Product not found."


class FakeScheduler:
    """Records the checks it is asked for and answers with a scripted outcome."""

    def __init__(
        self,
        result: CheckResult | None = None,
        error: Exception | None = None,
        new_price: Decimal | None = None,
        repo: Repository | None = None,
    ) -> None:
        self.calls: list[tuple[int, int]] = []
        self._result = result
        self._error = error
        self._new_price = new_price
        self._repo = repo

    async def check_one_product_for_user(self, *, product_id: int, user_id: int) -> CheckResult:
        self.calls.append((product_id, user_id))
        if self._error is not None:
            raise self._error
        if self._repo is not None and self._new_price is not None:
            await self._repo.update_price(product_id, self._new_price)
        return self._result or CheckResult(product_id=product_id, user_id=user_id)


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repository = Repository(conn)
    for user in (USER, OTHER):
        await repository.ensure_user(user)
    await repository.ensure_user(ADMIN, is_admin=True)
    try:
        yield repository
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def _english() -> Any:
    set_locale("en")
    yield
    set_locale("en")


async def add_product(repo: Repository, name: str, user_id: int = USER) -> int:
    return await repo.add_product(
        user_id=user_id,
        url=f"https://shop.example.com/{name.replace(' ', '-')}",
        name=name,
        domain="shop.example.com",
        initial_price=Decimal("80"),
        currency="EUR",
    )


def make_query(data: str, user_id: int, language: str) -> MagicMock:
    query = MagicMock()
    query.data = data
    query.from_user.id = user_id
    query.from_user.language_code = language
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message.reply_text = AsyncMock()
    return query


async def press(
    repo: Repository,
    data: str,
    *,
    user_id: int = USER,
    scheduler: Any = None,
    language: str = "en",
    query: MagicMock | None = None,
) -> MagicMock:
    query = query or make_query(data, user_id, language)
    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = user_id
    update.effective_user.language_code = language
    context = MagicMock()
    context.bot_data = {
        "db": repo,
        "repository": repo,
        "config": SimpleNamespace(check_interval_minutes=360),
        "scheduler": scheduler or FakeScheduler(),
    }
    context.user_data = {}
    await handle_callback(update, context)
    set_locale(language)
    return query


def shown(query: MagicMock) -> tuple[str, Any]:
    """The text and keyboard of the last edit of ``query``."""
    call = query.edit_message_text.await_args
    return call.args[0], call.kwargs.get("reply_markup")


def edited_texts(query: MagicMock) -> list[str]:
    return [str(c.args[0]) for c in query.edit_message_text.await_args_list]


def wires(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def with_notice(screen: Screen, notice: str) -> Screen:
    from dataclasses import replace

    return replace(screen, text=f"{notice}\n\n{screen.text}") if notice else screen


async def card_screen(repo: Repository, product_id: int, *, back: str) -> Screen:
    record = await repo.get_product(product_id)
    assert record is not None
    view = product_view(record, default_interval_minutes=360)
    return product_card(view, card_actions(view, back=back), now=datetime.now(UTC))


async def list_screen(
    repo: Repository, user_id: int = USER, list_filter: ListFilter = "a", page: int = 1
) -> Screen:
    from price_tracker.bot.ui.panels import home_button
    from price_tracker.bot.ui.screens import Screen

    records = await repo.get_all_products(user_id)
    if not records:
        return Screen(text=empty_list_text(), rows=((home_button(),),))
    return list_page(list_view(records, list_filter, page, default_interval_minutes=360))


def assert_shows(query: MagicMock, screen: Screen) -> None:
    text, markup = shown(query)
    assert text == screen.text
    assert markup == screen_markup(screen)


async def rows(repo: Repository) -> list[tuple[Any, ...]]:
    cursor = await repo._conn.execute("SELECT * FROM products ORDER BY id")
    return [tuple(row) for row in await cursor.fetchall()]


OLD_TEXTS = ("In pausa:", "Riattivato:", "Eliminato definitivamente", "Operazione annullata")


def assert_no_old_text(query: MagicMock) -> None:
    for text in edited_texts(query):
        assert not any(old in text for old in OLD_TEXTS), text


# --- pause and reactivate redraw the card -----------------------------------


async def test_pause_redraws_the_card_with_a_notice(repo: Repository) -> None:
    pid = await add_product(repo, "Kettle")
    query = await press(repo, f"pause_{pid}")
    record = await repo.get_product(pid)
    assert record is not None
    assert record.is_active is False
    expected = with_notice(await card_screen(repo, pid, back="l:p:1"), "⏸ Tracking paused.")
    assert_shows(query, expected)
    assert f"reactivate_{pid}" in wires(shown(query)[1])
    assert_no_old_text(query)


async def test_reactivate_redraws_the_card_with_a_notice(repo: Repository) -> None:
    pid = await add_product(repo, "Kettle")
    await repo.pause_product(pid)
    query = await press(repo, f"reactivate_{pid}")
    record = await repo.get_product(pid)
    assert record is not None
    assert record.is_active is True
    expected = with_notice(await card_screen(repo, pid, back="l:a:1"), "▶️ Tracking resumed.")
    assert_shows(query, expected)
    assert f"pause_{pid}" in wires(shown(query)[1])
    assert_no_old_text(query)


async def test_the_notices_are_translated(repo: Repository) -> None:
    pid = await add_product(repo, "Kettle")
    query = await press(repo, f"pause_{pid}", language="it")
    assert shown(query)[0].startswith("⏸ Monitoraggio in pausa.\n\n")
    query = await press(repo, f"reactivate_{pid}", language="it")
    assert shown(query)[0].startswith("▶️ Monitoraggio riattivato.\n\n")


async def test_a_repeated_pause_is_not_an_error(repo: Repository) -> None:
    pid = await add_product(repo, "Kettle")
    await press(repo, f"pause_{pid}")
    query = make_query(f"pause_{pid}", USER, "en")
    query.edit_message_text = AsyncMock(
        side_effect=BadRequest("Message is not modified: specified new message content")
    )
    await press(repo, f"pause_{pid}", query=query)
    query.edit_message_text.assert_awaited_once()


async def test_an_admin_pausing_a_foreign_product_sees_its_card(repo: Repository) -> None:
    foreign = await add_product(repo, "Secret Fan", user_id=OTHER)
    query = await press(repo, f"pause_{foreign}", user_id=ADMIN)
    text, markup = shown(query)
    assert "Secret Fan" in text
    assert text.startswith("⏸ Tracking paused.\n\n")
    assert f"reactivate_{foreign}" in wires(markup)


# --- the delete prompt and its Cancel ----------------------------------------


async def test_the_delete_prompt_cancels_back_to_the_card(repo: Repository) -> None:
    pid = await add_product(repo, "Kettle")
    prompt = await press(repo, f"remove_{pid}")
    assert wires(shown(prompt)[1]) == [f"confirm_delete_{pid}", f"pause_{pid}", f"p:{pid}:c"]
    query = await press(repo, f"p:{pid}:c")
    assert_shows(query, await card_screen(repo, pid, back="l:a:1"))
    assert await repo.get_product(pid) is not None
    assert_no_old_text(query)


async def test_pause_only_from_the_delete_prompt_shows_the_paused_card(repo: Repository) -> None:
    pid = await add_product(repo, "Kettle")
    await press(repo, f"remove_{pid}")
    query = await press(repo, f"pause_{pid}")
    assert_shows(
        query, with_notice(await card_screen(repo, pid, back="l:p:1"), "⏸ Tracking paused.")
    )


# --- deleting shows the list ------------------------------------------------


async def test_confirm_delete_shows_the_list_without_the_product(repo: Repository) -> None:
    pid = await add_product(repo, "Kettle")
    await add_product(repo, "Lamp")
    query = await press(repo, f"confirm_delete_{pid}")
    assert await repo.get_product(pid) is None
    assert_shows(query, with_notice(await list_screen(repo), "🗑 Deleted: Kettle"))
    assert "Kettle" not in shown(query)[0].split("\n\n", 1)[1]
    assert_no_old_text(query)


async def test_deleting_the_last_product_shows_the_empty_text_and_home(repo: Repository) -> None:
    pid = await add_product(repo, "Kettle")
    query = await press(repo, f"confirm_delete_{pid}")
    text, markup = shown(query)
    assert text == f"🗑 Deleted: Kettle\n\n{empty_list_text()}"
    assert wires(markup) == ["h"]


async def test_the_deleted_name_is_escaped_and_cut(repo: Repository) -> None:
    pid = await add_product(repo, "<b>" + "N" * 100)
    query = await press(repo, f"confirm_delete_{pid}")
    notice = shown(query)[0].split("\n\n", 1)[0]
    assert notice.startswith("🗑 Deleted: &lt;b&gt;NNN")
    assert notice.endswith("…")
    assert "<b>N" not in notice


async def test_the_delete_all_prompt_cancels_back_to_the_list(repo: Repository) -> None:
    await add_product(repo, "Kettle")
    await add_product(repo, "Lamp")
    prompt = await press(repo, "delete_all")
    assert wires(shown(prompt)[1]) == ["confirmdeleteall", "l:a:1"]
    query = await press(repo, "l:a:1")
    assert_shows(query, await list_screen(repo))
    assert len(await rows(repo)) == 2


@pytest.mark.parametrize(
    ("count", "language", "notice"),
    [
        (1, "en", "🗑 Deleted 1 product and its price history."),
        (2, "en", "🗑 Deleted 2 products and their price history."),
        (1, "it", "🗑 Eliminato 1 prodotto e il suo storico prezzi."),
        (2, "it", "🗑 Eliminati 2 prodotti e il loro storico prezzi."),
    ],
)
async def test_delete_all_shows_the_list_with_the_count(
    repo: Repository, count: int, language: str, notice: str
) -> None:
    for index in range(count):
        await add_product(repo, f"Item {index}")
    paused = await add_product(repo, "Paused")
    await repo.pause_product(paused)
    query = await press(repo, "confirmdeleteall", language=language)
    assert [row[0] for row in await rows(repo)] == [paused]
    assert_shows(query, with_notice(await list_screen(repo), notice))
    assert_no_old_text(query)


# --- Check now --------------------------------------------------------------


async def check(
    repo: Repository, pid: int, scheduler: FakeScheduler, **kwargs: Any
) -> tuple[MagicMock, Screen]:
    query = await press(repo, f"check_{pid}", scheduler=scheduler, **kwargs)
    return query, await card_screen(repo, pid, back="l:a:1")


async def test_check_without_change_redraws_the_card(repo: Repository) -> None:
    pid = await add_product(repo, "Kettle")
    scheduler = FakeScheduler()
    query, card = await check(repo, pid, scheduler)
    assert scheduler.calls == [(pid, USER)]
    assert edited_texts(query)[0] == "🔄 Checking..."
    assert_shows(query, with_notice(card, "✅ Checked: no significant change."))
    assert f"check_{pid}" in wires(shown(query)[1])


async def test_check_with_a_drop_shows_it_and_the_fresh_card(repo: Repository) -> None:
    pid = await add_product(repo, "Kettle")
    alert = PriceAlert(
        product_id=pid,
        product_name="Kettle",
        url="https://shop.example.com/Kettle",
        old_price=Decimal("80"),
        new_price=Decimal("64"),
        currency="EUR",
        threshold_type="percentage",
        threshold_value=Decimal("10"),
    )
    scheduler = FakeScheduler(
        CheckResult(product_id=pid, user_id=USER, alert=alert),
        new_price=Decimal("64"),
        repo=repo,
    )
    query, card = await check(repo, pid, scheduler)
    assert_shows(query, with_notice(card, "🔔 Price dropped: €80.00 → €64.00"))
    assert "💰 Now: €64.00" in shown(query)[0]


async def test_check_with_a_drop_in_italian(repo: Repository) -> None:
    pid = await add_product(repo, "Kettle")
    alert = PriceAlert(
        product_id=pid,
        product_name="Kettle",
        url="https://shop.example.com/Kettle",
        old_price=Decimal("80"),
        new_price=Decimal("64"),
        currency="EUR",
        threshold_type="percentage",
        threshold_value=Decimal("10"),
    )
    scheduler = FakeScheduler(CheckResult(product_id=pid, user_id=USER, alert=alert))
    query, _ = await check(repo, pid, scheduler, language="it")
    assert edited_texts(query)[0] == "🔄 Controllo in corso..."
    assert shown(query)[0].startswith("🔔 Prezzo sceso: 80,00\xa0€ → 64,00\xa0€\n\n")


async def test_a_drop_money_cannot_render_still_redraws_the_card(repo: Repository) -> None:
    pid = await add_product(repo, "Kettle")
    alert = PriceAlert(
        product_id=pid,
        product_name="Kettle",
        url="https://shop.example.com/Kettle",
        old_price=Decimal("1e20"),
        new_price=Decimal("64"),
        currency="EUR",
        threshold_type="percentage",
        threshold_value=Decimal("10"),
    )
    scheduler = FakeScheduler(CheckResult(product_id=pid, user_id=USER, alert=alert))
    query, card = await check(repo, pid, scheduler)
    assert_shows(query, with_notice(card, "🔔 Price dropped: — → €64.00"))


async def test_check_out_of_stock_says_so(repo: Repository) -> None:
    pid = await add_product(repo, "Kettle")
    scheduler = FakeScheduler(CheckResult(product_id=pid, user_id=USER, reason="out_of_stock"))
    query, card = await check(repo, pid, scheduler)
    assert_shows(query, with_notice(card, "📦 Out of stock - I will tell you when it is back."))


async def test_the_drop_is_shown_in_the_product_currency(repo: Repository) -> None:
    pid = await repo.add_product(
        user_id=USER,
        url="https://shop.example.com/lamp",
        name="Lamp",
        domain="shop.example.com",
        initial_price=Decimal("80"),
        currency="USD",
    )
    alert = PriceAlert(
        product_id=pid,
        product_name="Lamp",
        url="https://shop.example.com/lamp",
        old_price=Decimal("80"),
        new_price=Decimal("64"),
        currency="USD",
        threshold_type="percentage",
        threshold_value=Decimal("10"),
    )
    scheduler = FakeScheduler(CheckResult(product_id=pid, user_id=USER, alert=alert))
    query, _ = await check(repo, pid, scheduler)
    assert shown(query)[0].startswith("🔔 Price dropped: $80.00 → $64.00\n\n")


async def test_check_that_could_not_read_says_why(repo: Repository) -> None:
    pid = await add_product(repo, "Kettle")
    scheduler = FakeScheduler(CheckResult(product_id=pid, user_id=USER, reason="parse_error"))
    query, card = await check(repo, pid, scheduler)
    assert_shows(query, with_notice(card, "❌ Not updated: price not readable"))


async def test_a_gone_listing_reports_the_status_the_check_saw(repo: Repository) -> None:
    pid = await add_product(repo, "Kettle")
    await repo.record_failure(pid, reason="listing_gone", detail="HTTP 410")
    scheduler = FakeScheduler(CheckResult(product_id=pid, user_id=USER, reason="listing_gone"))
    query, card = await check(repo, pid, scheduler)
    assert_shows(query, with_notice(card, "❌ Not updated: page not found (HTTP 410)"))


async def test_a_failing_check_does_not_show_the_error_text(
    repo: Repository, caplog: pytest.LogCaptureFixture
) -> None:
    pid = await add_product(repo, "Kettle")
    scheduler = FakeScheduler(error=RuntimeError("secret boom at /internal/path"))
    with caplog.at_level(logging.WARNING):
        query, card = await check(repo, pid, scheduler)
    assert_shows(query, with_notice(card, "❌ Could not check this product. Try again later."))
    assert not any("secret boom" in text for text in edited_texts(query))
    assert [r.levelno for r in caplog.records if r.levelno >= logging.WARNING] == [logging.WARNING]


async def test_a_failing_check_logs_its_traceback(
    repo: Repository, caplog: pytest.LogCaptureFixture
) -> None:
    pid = await add_product(repo, "Kettle")
    scheduler = FakeScheduler(error=TypeError("a programming bug"))
    with caplog.at_level(logging.WARNING):
        await check(repo, pid, scheduler)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None


async def test_an_admin_checking_a_foreign_product_does_not_claim_a_check(
    repo: Repository,
) -> None:
    foreign = await add_product(repo, "Secret Fan", user_id=OTHER)
    before = await rows(repo)
    scheduler = FakeScheduler()
    query = await press(repo, f"check_{foreign}", user_id=ADMIN, scheduler=scheduler)
    assert scheduler.calls == []
    assert edited_texts(query) == [shown(query)[0]]
    expected = with_notice(
        await card_screen(repo, foreign, back="l:a:1"),
        "⛔ This product belongs to another user: it was not checked.",
    )
    assert_shows(query, expected)
    assert "Checked" not in shown(query)[0]
    assert await rows(repo) == before


@pytest.mark.parametrize("kind", ["manual", "automatic"])
async def test_check_on_an_inactive_product_does_not_check(repo: Repository, kind: str) -> None:
    pid = await add_product(repo, "Kettle")
    await repo.pause_product(pid)
    if kind == "automatic":
        await repo._conn.execute(
            "UPDATE products SET suspension_kind = 'automatic' WHERE id = ?", (pid,)
        )
        await repo._conn.commit()
    before = await rows(repo)
    scheduler = FakeScheduler()
    query = await press(repo, f"check_{pid}", scheduler=scheduler)
    assert scheduler.calls == []
    assert edited_texts(query) == [shown(query)[0]]
    expected = with_notice(
        await card_screen(repo, pid, back="l:p:1"),
        "⏸ Not checked: tracking is paused. Reactivate it first.",
    )
    assert_shows(query, expected)
    assert await rows(repo) == before


async def test_a_product_deleted_during_the_check_shows_the_list(repo: Repository) -> None:
    pid = await add_product(repo, "Kettle")

    class Deleting(FakeScheduler):
        async def check_one_product_for_user(self, *, product_id: int, user_id: int) -> CheckResult:
            await repo.delete_product(product_id, user_id=user_id)
            return CheckResult(product_id=product_id, user_id=user_id)

    query = await press(repo, f"check_{pid}", scheduler=Deleting())
    assert_shows(query, with_notice(await list_screen(repo), NOT_FOUND))


# --- stale and not-well-formed presses ---------------------------------------

MALFORMED_CARD = [
    "p:0:c",
    "p:01:c",
    "p:-1:c",
    "p:9223372036854775808:c",
    "p:12:C",
    "p:12:c:x",
    "p:12:",
    "p::c",
    "p:\u0661\u0662:c",
]


@pytest.mark.parametrize("data", MALFORMED_CARD)
async def test_a_malformed_card_callback_edits_and_writes_nothing(
    repo: Repository, data: str
) -> None:
    await add_product(repo, "Kettle")
    assert isinstance(decode(data), InvalidCallback)
    before = await rows(repo)
    query = await press(repo, data)
    query.edit_message_text.assert_not_called()
    assert await rows(repo) == before


@pytest.mark.parametrize("owner", ["nobody", "other"])
async def test_a_card_that_is_not_mine_is_not_found_and_not_revealed(
    repo: Repository, owner: str
) -> None:
    await add_product(repo, "Mine")
    pid = 999 if owner == "nobody" else await add_product(repo, "Secret Fan", user_id=OTHER)
    query = await press(repo, f"p:{pid}:c")
    assert_shows(query, with_notice(await list_screen(repo), NOT_FOUND))
    assert "Secret Fan" not in shown(query)[0]


LEGACY_ACTIONS = ["check_{}", "pause_{}", "reactivate_{}", "remove_{}", "confirm_delete_{}"]


@pytest.mark.parametrize("pattern", LEGACY_ACTIONS)
@pytest.mark.parametrize("owner", ["nobody", "other"])
async def test_a_legacy_action_on_a_product_that_is_not_mine_shows_the_list(
    repo: Repository, pattern: str, owner: str
) -> None:
    await add_product(repo, "Mine")
    foreign = await add_product(repo, "Secret Fan", user_id=OTHER)
    if pattern.startswith("reactivate"):
        await repo.pause_product(foreign)
    pid = 999 if owner == "nobody" else foreign
    before = await rows(repo)
    scheduler = FakeScheduler()
    query = await press(repo, pattern.format(pid), scheduler=scheduler)
    assert_shows(query, with_notice(await list_screen(repo), NOT_FOUND))
    assert "Secret Fan" not in shown(query)[0]
    assert await rows(repo) == before
    assert scheduler.calls == []


@pytest.mark.parametrize(
    "data", ["check_x", "pause_0", "remove_-1", "confirm_delete_9223372036854775808"]
)
async def test_a_malformed_id_keeps_its_reply(repo: Repository, data: str) -> None:
    await add_product(repo, "Kettle")
    before = await rows(repo)
    query = await press(repo, data)
    query.edit_message_text.assert_awaited_once_with(BAD_ID)
    assert await rows(repo) == before
