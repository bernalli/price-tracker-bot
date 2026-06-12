"""Per-product callbacks must enforce ownership (IDOR guard).

``handle_reactivate_button`` looked the product up via ``db.get_product``
(no user filter) while every sibling action handler goes through
``_get_user_product`` (owner-or-admin). Since ``reactivate_product`` does not
filter by user_id either, any user could reactivate another user's product
(and reset its ``consecutive_errors``) by sending ``reactivate_<foreign id>``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.callbacks import _actions

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
    return db


def _mock_context(db: AsyncMock) -> MagicMock:
    context = MagicMock()
    context.bot_data = {"db": db}
    context.user_data = {}
    return context


@pytest.mark.asyncio
async def test_reactivate_foreign_product_is_rejected() -> None:
    """Non-owner sends ``reactivate_<foreign id>`` → no reactivation, error reply."""
    db = _mock_db(is_admin=False, owned_product=None)  # not visible to this user
    query = _mock_query()
    context = _mock_context(db)

    handled = await _actions.handle_reactivate_button(
        query, context, db, INTRUDER_ID, f"reactivate_{PRODUCT_ID}"
    )

    assert handled is True
    db.reactivate_product.assert_not_awaited()
    query.edit_message_text.assert_awaited_once_with("❌ Prodotto non trovato.")


@pytest.mark.asyncio
async def test_reactivate_own_product_succeeds() -> None:
    """Owner sends ``reactivate_<id>`` → product reactivated, confirmation reply."""
    db = _mock_db(is_admin=False, owned_product={"name": "Widget"})
    query = _mock_query()
    context = _mock_context(db)

    handled = await _actions.handle_reactivate_button(
        query, context, db, OWNER_ID, f"reactivate_{PRODUCT_ID}"
    )

    assert handled is True
    db.get_product_for_user.assert_awaited_once_with(PRODUCT_ID, OWNER_ID)
    db.reactivate_product.assert_awaited_once_with(PRODUCT_ID)
    msg = query.edit_message_text.await_args.args[0]
    assert "Riattivato" in msg
