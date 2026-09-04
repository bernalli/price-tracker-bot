"""Operational-notice callback handlers keep bulk actions narrowly scoped."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio

from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "src" / "price_tracker" / "db" / "migrations"
USER_ID = 100
OTHER_USER_ID = 200


def _query() -> MagicMock:
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    query.message.reply_text = AsyncMock()
    return query


def _context(repo: Repository, scheduler: AsyncMock) -> MagicMock:
    context = MagicMock()
    context.bot_data = {"db": repo, "scheduler": scheduler}
    return context


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    """Migrated repository so group filtering exercises the SQL contract."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    try:
        yield Repository(conn)
    finally:
        await conn.close()


async def _add_product(repo: Repository, *, user_id: int, name: str, url: str) -> int:
    await repo.add_user(user_id)
    return await repo.add_product(
        user_id=user_id,
        name=name,
        url=url,
        domain=None,
        initial_price=Decimal("10"),
        currency="EUR",
    )


async def _automatic_product(
    repo: Repository,
    *,
    user_id: int,
    name: str,
    url: str,
    reason: str = "listing_gone",
    failures: int = 0,
    failure_reason: str = "http_error",
) -> int:
    product_id = await _add_product(repo, user_id=user_id, name=name, url=url)
    for _ in range(failures):
        await repo.record_failure(product_id, reason=failure_reason)
    assert await repo.suspend_product(product_id, reason=reason)
    return product_id


@pytest.mark.asyncio
async def test_ops_react_rejects_invalid_missing_and_foreign_anchor(repo: Repository) -> None:
    """Callback parsing and anchor ownership fail closed before any mutation."""
    from price_tracker.bot.handlers.callbacks import _ops

    scheduler = AsyncMock()
    query = _query()
    context = _context(repo, scheduler)

    assert await _ops.handle_ops_buttons(query, context, repo, USER_ID, "ops_react_x") is True
    query.edit_message_text.assert_awaited_once_with("❌ Invalid ID.")

    query.edit_message_text.reset_mock()
    assert await _ops.handle_ops_buttons(query, context, repo, USER_ID, "ops_react_999") is True
    query.edit_message_text.assert_awaited_once_with("❌ Product not found.")

    foreign_id = await _automatic_product(
        repo, user_id=OTHER_USER_ID, name="Foreign", url="https://a.example.com/foreign"
    )
    query.edit_message_text.reset_mock()
    assert (
        await _ops.handle_ops_buttons(query, context, repo, USER_ID, f"ops_react_{foreign_id}")
        is True
    )
    query.edit_message_text.assert_awaited_once_with("❌ Product not found.")
    scheduler.check_products_for_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_ops_react_acts_only_on_automatic_suspensions_of_same_domain(
    repo: Repository,
) -> None:
    """Manual, unknown, foreign, and other-domain inactive rows are never bulk-reactivated."""
    from price_tracker.bot.handlers.callbacks import _ops

    automatic_ids = [
        await _automatic_product(
            repo,
            user_id=USER_ID,
            name=f"Automatic {index}",
            url=f"https://a.example.com/{index}",
            failures=10,
        )
        for index in range(3)
    ]
    automatic_ids.extend(
        [
            await _automatic_product(
                repo,
                user_id=USER_ID,
                name=f"Gone {index}",
                url=f"https://shop.a.example.com/gone-{index}",
                failures=3,
                failure_reason="listing_gone",
            )
            for index in range(2)
        ]
    )
    manual_id = await _add_product(
        repo, user_id=USER_ID, name="Manual", url="https://a.example.com/manual"
    )
    for _ in range(7):
        await repo.record_failure(manual_id, reason="http_error")
    await repo.pause_product(manual_id)

    unknown_id = await _add_product(
        repo, user_id=USER_ID, name="Unknown", url="https://a.example.com/unknown"
    )
    await repo._conn.execute(  # noqa: SLF001 - migration-era row is the contract under test.
        "UPDATE products SET is_active = 0, consecutive_errors = 10 WHERE id = ?", (unknown_id,)
    )
    await repo._conn.commit()  # noqa: SLF001 - see direct fixture update above.
    other_domain_id = await _automatic_product(
        repo, user_id=USER_ID, name="Other domain", url="https://b.otherdomain.com/one"
    )
    foreign_id = await _automatic_product(
        repo, user_id=OTHER_USER_ID, name="Foreign", url="https://a.example.com/other"
    )

    scheduler = AsyncMock()
    scheduler.check_products_for_user.return_value = [
        SimpleNamespace(reason=None),
        SimpleNamespace(reason="listing_gone"),
        SimpleNamespace(reason=None),
        SimpleNamespace(reason="listing_gone"),
        SimpleNamespace(reason=None),
    ]
    query = _query()
    context = _context(repo, scheduler)
    context.bot_data["config"] = SimpleNamespace(max_consecutive_errors=100)

    handled = await _ops.handle_ops_buttons(
        query, context, repo, USER_ID, f"ops_react_{automatic_ids[0]}"
    )

    assert handled is True
    assert scheduler.check_products_for_user.await_args.kwargs == {
        "product_ids": automatic_ids,
        "user_id": USER_ID,
        "delay_between_products": 0.5,
    }
    for product_id in automatic_ids:
        assert (await repo.get_product(product_id)).is_active is True  # type: ignore[union-attr]
    for product_id in (manual_id, unknown_id, other_domain_id, foreign_id):
        assert (await repo.get_product(product_id)).is_active is False  # type: ignore[union-attr]

    final_text = query.edit_message_text.await_args_list[-1].args[0]
    assert final_text.count("✅") == 3
    assert final_text.count("❌") == 2
    assert final_text.count("page not found") == 2


@pytest.mark.asyncio
async def test_ops_react_result_is_independent_of_thresholds(repo: Repository) -> None:
    """The callback never reads a configured error threshold to define its group."""
    from price_tracker.bot.handlers.callbacks import _ops

    product_id = await _automatic_product(
        repo, user_id=USER_ID, name="Automatic", url="https://a.example.com/one"
    )
    scheduler = AsyncMock()
    scheduler.check_products_for_user.return_value = [SimpleNamespace(reason=None)]
    context = _context(repo, scheduler)
    context.bot_data["config"] = SimpleNamespace(max_consecutive_errors=100)

    await _ops.handle_ops_buttons(_query(), context, repo, USER_ID, f"ops_react_{product_id}")

    assert scheduler.check_products_for_user.await_args.kwargs["product_ids"] == [product_id]


@pytest.mark.asyncio
async def test_ops_react_empty_group_says_nothing_to_do(repo: Repository) -> None:
    """A changed group is re-read and a non-automatic anchor cannot trigger a bulk operation."""
    from price_tracker.bot.handlers.callbacks import _ops

    product_id = await _add_product(
        repo, user_id=USER_ID, name="Manual", url="https://a.example.com/manual"
    )
    await repo.pause_product(product_id)
    scheduler = AsyncMock()
    query = _query()

    assert (
        await _ops.handle_ops_buttons(
            query, _context(repo, scheduler), repo, USER_ID, f"ops_react_{product_id}"
        )
        is True
    )
    query.edit_message_text.assert_awaited_once_with(
        "❌ Nothing to do: no automatically suspended products on this site."
    )


@pytest.mark.asyncio
async def test_ops_react_reply_is_chunked(repo: Repository) -> None:
    """Long recheck summaries obey the Telegram visible-length limit."""
    from price_tracker.bot.handlers.callbacks import _ops
    from price_tracker.core.textlimits import visible_length

    product_ids = [
        await _automatic_product(
            repo,
            user_id=USER_ID,
            name=f"{index}-" + "x" * 200,
            url=f"https://a.example.com/{index}",
        )
        for index in range(60)
    ]
    scheduler = AsyncMock()
    scheduler.check_products_for_user.return_value = [SimpleNamespace(reason=None)] * 60
    query = _query()
    query.answer = AsyncMock()

    await _ops.handle_ops_buttons(
        query, _context(repo, scheduler), repo, USER_ID, f"ops_react_{product_ids[0]}"
    )

    chunks = [call.args[0] for call in query.edit_message_text.await_args_list[1:]]
    chunks.extend(call.args[0] for call in query.message.reply_text.await_args_list)
    assert len(chunks) >= 2
    assert all(visible_length(chunk) <= 4000 for chunk in chunks)


@pytest.mark.asyncio
async def test_ops_del_shows_confirmation_with_count_and_cancel(repo: Repository) -> None:
    """Delete prompt retains the stable anchor but reports the current group size."""
    from price_tracker.bot.handlers.callbacks import _ops

    first_id = await _automatic_product(
        repo, user_id=USER_ID, name="One", url="https://a.example.com/one"
    )
    await _automatic_product(repo, user_id=USER_ID, name="Two", url="https://a.example.com/two")
    query = _query()

    assert (
        await _ops.handle_ops_buttons(
            query, _context(repo, AsyncMock()), repo, USER_ID, f"ops_del_{first_id}"
        )
        is True
    )

    text = query.edit_message_text.await_args.args[0]
    markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert "Delete 2 products on example.com" in text
    assert markup.inline_keyboard[0][0].callback_data == f"ops_delok_{first_id}"
    assert markup.inline_keyboard[1][0].callback_data == "cancel_delete"


@pytest.mark.asyncio
async def test_ops_delok_deletes_group_and_reports_count(repo: Repository) -> None:
    """Confirmed deletion is constrained to the automatic same-domain group."""
    from price_tracker.bot.handlers.callbacks import _ops

    first_id = await _automatic_product(
        repo, user_id=USER_ID, name="One", url="https://a.example.com/one"
    )
    second_id = await _automatic_product(
        repo, user_id=USER_ID, name="Two", url="https://a.example.com/two"
    )
    query = _query()

    assert (
        await _ops.handle_ops_buttons(
            query, _context(repo, AsyncMock()), repo, USER_ID, f"ops_delok_{first_id}"
        )
        is True
    )

    assert await repo.get_product(first_id) is None
    assert await repo.get_product(second_id) is None
    assert "Deleted 2 products on example.com" in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_ops_delok_recomputes_group(repo: Repository) -> None:
    """The confirmation click uses current provenance, not the stale prompt group."""
    from price_tracker.bot.handlers.callbacks import _ops

    first_id = await _automatic_product(
        repo, user_id=USER_ID, name="One", url="https://a.example.com/one"
    )
    second_id = await _automatic_product(
        repo, user_id=USER_ID, name="Two", url="https://a.example.com/two"
    )
    await repo.reactivate_product(second_id)

    await _ops.handle_ops_buttons(
        _query(), _context(repo, AsyncMock()), repo, USER_ID, f"ops_delok_{first_id}"
    )

    assert await repo.get_product(first_id) is None
    assert await repo.get_product(second_id) is not None


@pytest.mark.asyncio
async def test_ops_delok_never_deletes_manual_or_unknown_provenance(repo: Repository) -> None:
    """A manual pause and a pre-migration unknown row remain untouched by bulk delete."""
    from price_tracker.bot.handlers.callbacks import _ops

    automatic_id = await _automatic_product(
        repo, user_id=USER_ID, name="Automatic", url="https://a.example.com/one"
    )
    manual_id = await _add_product(
        repo, user_id=USER_ID, name="Manual", url="https://a.example.com/manual"
    )
    await repo.pause_product(manual_id)
    unknown_id = await _add_product(
        repo, user_id=USER_ID, name="Unknown", url="https://a.example.com/unknown"
    )
    await repo._conn.execute("UPDATE products SET is_active = 0 WHERE id = ?", (unknown_id,))  # noqa: SLF001
    await repo._conn.commit()  # noqa: SLF001

    await _ops.handle_ops_buttons(
        _query(), _context(repo, AsyncMock()), repo, USER_ID, f"ops_delok_{automatic_id}"
    )

    assert await repo.get_product(automatic_id) is None
    assert await repo.get_product(manual_id) is not None
    assert await repo.get_product(unknown_id) is not None


@pytest.mark.asyncio
async def test_dispatcher_routes_ops_prefix_before_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operational handler precedes legacy action callbacks in dispatcher order."""
    import price_tracker.bot.handlers.callbacks as callbacks

    query = _query()
    query.answer = AsyncMock()
    query.from_user.id = USER_ID
    query.data = "ops_react_1"
    update = MagicMock(callback_query=query, effective_user=MagicMock(language_code="en"))
    context = MagicMock()
    context.bot_data = {"db": AsyncMock(is_user_allowed=AsyncMock(return_value=True))}
    ops = AsyncMock(return_value=True)
    actions = AsyncMock(return_value=True)
    monkeypatch.setattr(callbacks._ops, "handle_ops_buttons", ops)
    monkeypatch.setattr(callbacks._actions, "handle_edit_button", actions)

    await callbacks.handle_callback(update, context)

    ops.assert_awaited_once()
    actions.assert_not_awaited()
