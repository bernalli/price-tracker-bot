"""Per-product callbacks must enforce ownership (IDOR guard) and persist.

``handle_reactivate_button`` looked the product up via ``db.get_product``
(no user filter) while every sibling action handler goes through
``_get_user_product`` (owner-or-admin). Since ``reactivate_product`` does not
filter by user_id either, any user could reactivate another user's product
(and reset its ``consecutive_errors``) by sending ``reactivate_<foreign id>``.

``track_default_`` showed the "-10%" confirmation without ever calling
``set_threshold``: an ``any_drop`` product silently kept notifying on every
variation while the user believed the -10% default was active.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.callbacks import _actions, _product
from price_tracker.bot.messages import set_locale
from tests.support.list_variants import record

OWNER_ID = 1
INTRUDER_ID = 2
PRODUCT_ID = 42


def _mock_query() -> MagicMock:
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    query.message.reply_text = AsyncMock()
    return query


def _mock_db(*, is_admin: bool, owned_product: dict[str, Any] | None) -> AsyncMock:
    """DB mock wired for ``_get_user_product``: admin bypass + per-user lookup."""
    db = AsyncMock()
    db.is_user_admin.return_value = is_admin
    db.get_product_for_user.return_value = owned_product
    db.get_product.return_value = owned_product or {"name": "Foreign Widget"}
    db.get_all_products.return_value = []
    db.get_config.return_value = None
    return db


def _mock_context(db: AsyncMock) -> MagicMock:
    context = MagicMock()
    context.bot_data = {"db": db, "config": SimpleNamespace(check_interval_minutes=360)}
    context.user_data = {}
    return context


@pytest.mark.asyncio
async def test_reactivate_foreign_product_is_rejected() -> None:
    """Non-owner sends ``reactivate_<foreign id>`` → no reactivation, error reply."""
    db = _mock_db(is_admin=False, owned_product=None)  # not visible to this user
    query = _mock_query()
    context = _mock_context(db)
    set_locale("en")

    handled = await _actions.handle_reactivate_button(
        query, context, db, INTRUDER_ID, f"reactivate_{PRODUCT_ID}"
    )

    assert handled is True
    db.reactivate_product.assert_not_awaited()
    query.edit_message_text.assert_awaited_once()
    msg = query.edit_message_text.await_args.args[0]
    assert msg.startswith("Product not found.\n\n")
    assert "Foreign Widget" not in msg


@pytest.mark.asyncio
async def test_reactivate_own_product_succeeds() -> None:
    """Owner sends ``reactivate_<id>`` → product reactivated, confirmation reply."""
    db = _mock_db(is_admin=False, owned_product=record(PRODUCT_ID, name="Widget"))
    query = _mock_query()
    context = _mock_context(db)
    set_locale("en")

    handled = await _actions.handle_reactivate_button(
        query, context, db, OWNER_ID, f"reactivate_{PRODUCT_ID}"
    )

    assert handled is True
    db.get_product_for_user.assert_awaited_once_with(PRODUCT_ID, OWNER_ID)
    db.reactivate_product.assert_awaited_once_with(PRODUCT_ID)
    msg = query.edit_message_text.await_args.args[0]
    assert msg.startswith("▶️ Tracking resumed.\n\n")
    assert "Widget" in msg


@pytest.mark.asyncio
async def test_track_default_persists_percentage_threshold() -> None:
    """Owner taps "Default -10%" → threshold actually written before confirming."""
    db = _mock_db(is_admin=False, owned_product={"name": "Widget"})
    query = _mock_query()
    context = _mock_context(db)

    handled = await _product.handle_track_choice(
        query, context, db, OWNER_ID, f"track_default_{PRODUCT_ID}"
    )

    assert handled is True
    db.set_threshold.assert_awaited_once_with(PRODUCT_ID, "percentage", "10")
    msg = query.edit_message_text.await_args.args[0]
    assert "-10%" in msg


@pytest.mark.asyncio
async def test_track_default_foreign_product_writes_nothing() -> None:
    """Non-owner sends ``track_default_<foreign id>`` → no write, error reply."""
    db = _mock_db(is_admin=False, owned_product=None)
    query = _mock_query()
    context = _mock_context(db)

    handled = await _product.handle_track_choice(
        query, context, db, INTRUDER_ID, f"track_default_{PRODUCT_ID}"
    )

    assert handled is True
    db.set_threshold.assert_not_awaited()
    query.edit_message_text.assert_awaited_once_with("❌ Prodotto non trovato.")
