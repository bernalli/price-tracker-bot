"""A product kept unread by a refusing store is marked as locked, not as failing."""

from __future__ import annotations

from dataclasses import replace

import pytest

from price_tracker.bot.ui.cards import list_mark
from price_tracker.bot.ui.product_rows import record_row
from tests.support.card_variants import VARIANTS

ACTIVE = VARIANTS["sold_out"]


@pytest.mark.parametrize("outcome", ["block", "quarantined", "probe_skipped"])
def test_store_refusal_is_marked_with_a_lock(outcome: str) -> None:
    view = replace(ACTIVE, out_of_stock=False, consecutive_errors=0, last_check_outcome=outcome)
    assert list_mark(view) == "🔒"
    row = record_row({"name": "Item", "current_price": "10", "last_check_outcome": outcome})
    assert "🔒" in row
    assert "⚠️" not in row


def test_store_refusal_outranks_a_stale_error_counter() -> None:
    view = replace(
        ACTIVE, out_of_stock=False, consecutive_errors=3, last_check_outcome="quarantined"
    )
    assert list_mark(view) == "🔒"
    row = record_row(
        {
            "name": "Item",
            "current_price": "10",
            "consecutive_errors": 3,
            "last_check_outcome": "quarantined",
        }
    )
    assert "🔒" in row


def test_paused_product_keeps_the_pause_mark() -> None:
    view = replace(ACTIVE, status="paused", out_of_stock=False, last_check_outcome="block")
    assert list_mark(view) == "⏸"
    row = record_row(
        {"name": "Item", "current_price": "10", "is_active": 0, "last_check_outcome": "block"}
    )
    assert "⏸" in row


@pytest.mark.parametrize("outcome", ["price_none", "deferred", "currency_mismatch"])
def test_other_outcomes_keep_the_warning_mark(outcome: str) -> None:
    view = replace(ACTIVE, out_of_stock=False, consecutive_errors=0, last_check_outcome=outcome)
    assert list_mark(view) == "⚠️"
