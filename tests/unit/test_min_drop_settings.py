"""Pressing a minimum-drop preset stores it and redraws the section."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from price_tracker.bot.handlers.callbacks import handle_callback
from price_tracker.core.drop_floor import DropFloor
from tests.unit.test_nav_dispatch import USER, edited_text, make_context, make_query, make_update
from tests.unit.test_nav_dispatch import repo as repo  # noqa: F401 — the shared fixture

if TYPE_CHECKING:
    from price_tracker.db.repository import Repository


async def _press(repo: Repository, data: str, *, floor: DropFloor | None = None) -> str:
    query = make_query(data)
    context = make_context(repo)
    if floor is not None:
        context.bot_data["scheduler"] = SimpleNamespace(deps=SimpleNamespace(drop_floor=floor))
    await handle_callback(make_update(query), context)
    query.edit_message_text.assert_awaited_once()
    return edited_text(query)


@pytest.mark.parametrize("choice", ["off", "1", "2", "5"])
async def test_a_preset_is_stored_and_marked(repo: Repository, choice: str) -> None:
    text = await _press(repo, f"s:md:{choice}")
    assert await repo.get_user_min_drop(USER) == choice
    assert text.startswith("📉 <b>Minimum drop</b>")


async def test_the_section_and_overview_show_the_running_default(repo: Repository) -> None:
    floor = DropFloor(Decimal(3), Decimal("0.5"))
    assert (await _press(repo, "s:md", floor=floor)).endswith("Current: 3% and 0.5 (default)")
    assert "📉 Minimum drop: 3% and 0.5 (default)" in await _press(repo, "s", floor=floor)


async def test_without_a_scheduler_the_documented_default_is_shown(repo: Repository) -> None:
    assert (await _press(repo, "s:md")).endswith("Current: 2% and 1 (default)")


async def test_the_last_press_wins(repo: Repository) -> None:
    for wire in ("s:md:5", "s:md:off", "s:md:1"):
        await _press(repo, wire)
    assert await repo.get_user_min_drop(USER) == "1"
