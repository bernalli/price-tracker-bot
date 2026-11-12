"""Error reports retain the accepted price through the repository projection."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from price_tracker.bot.handlers.debug import errors_text
from price_tracker.bot.messages import reset_locale, set_locale
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    import aiosqlite


@pytest.fixture
async def repo(memory_db: aiosqlite.Connection) -> Repository:
    """Use migrated in-memory storage without opening a persisted database."""
    await apply_migrations(memory_db, Path("src/price_tracker/db/migrations"))
    return Repository(memory_db)


@pytest.mark.parametrize("suspended", [False, True])
@pytest.mark.parametrize(
    ("locale", "currency", "price", "shown", "label"),
    [
        ("en", "USD", "79.95", "$79.95", "Last recorded price:"),
        ("it", "EUR", "79.95", "79,95\u00a0€", "Ultimo prezzo registrato:"),
        ("en", "GBP", "0", "£0.00", "Last recorded price:"),
    ],
)
async def test_errors_show_last_accepted_price_in_its_currency(
    repo: Repository,
    suspended: bool,
    locale: str,
    currency: str,
    price: str,
    shown: str,
    label: str,
) -> None:
    pid = await repo.add_product(
        user_id=7,
        url="https://shop.example/item",
        name="Kettle",
        domain="shop.example",
        initial_price=Decimal("100"),
        currency=currency,
    )
    await repo.update_price(pid, Decimal(price))
    await repo.record_failure(pid, reason="parse_error", detail="PRIVATE diagnostic")
    if suspended:
        await repo.suspend_product(pid, reason="parse_error")

    token = set_locale(locale)
    try:
        text = await errors_text(repo, None, 7)
        assert text is not None
        assert f"<b>{shown}</b>" in text
        assert label in text
        assert f"#{pid}:" in text
        assert "PRIVATE" not in text
        assert "N/A" not in text
        assert "N/D" not in text
        assert await errors_text(repo, None, 8) is None
    finally:
        reset_locale(token)


@pytest.mark.parametrize(("locale", "shown"), [("en", "N/A"), ("it", "N/D")])
async def test_errors_keep_missing_price_unavailable(
    repo: Repository, locale: str, shown: str
) -> None:
    pid = await repo.add_product(
        user_id=7,
        url="https://shop.example/item",
        name="No price",
        domain="shop.example",
        initial_price=None,
        currency="USD",
    )
    await repo.record_failure(pid, reason="price_none")
    token = set_locale(locale)
    try:
        text = await errors_text(repo, None, 7)
        assert text is not None
        assert f"<b>{shown}</b>" in text
        assert f"#{pid}:" in text
    finally:
        reset_locale(token)


async def test_store_refusal_keeps_last_recorded_price_without_product_error(
    repo: Repository,
) -> None:
    pid = await repo.add_product(
        user_id=7,
        url="https://shop.example/item",
        name="Blocked store",
        domain="shop.example",
        initial_price=Decimal("25.50"),
        currency="USD",
    )
    await repo.record_block(pid)
    token = set_locale("en")
    try:
        text = await errors_text(repo, None, 7)
        assert text is not None
        assert "<b>$25.50</b> 🔒" in text
        assert "Last recorded price:" in text
        assert "⚠️0" not in text
    finally:
        reset_locale(token)


@pytest.mark.parametrize("stored", ["abc", "", "12,50", "NaN", "Infinity", "-5", "1E+20"])
async def test_a_malformed_stored_price_reads_as_missing_and_keeps_the_report(
    repo: Repository, memory_db: aiosqlite.Connection, stored: str
) -> None:
    pid = await repo.add_product(
        user_id=7,
        url="https://shop.example/item",
        name="Legacy row",
        domain="shop.example",
        initial_price=Decimal("10"),
        currency="EUR",
    )
    await repo.record_failure(pid, reason="parse_error")
    await memory_db.execute("UPDATE products SET current_price = ? WHERE id = ?", (stored, pid))
    await memory_db.commit()
    token = set_locale("en")
    try:
        text = await errors_text(repo, None, 7)
        assert text is not None
        assert "<b>N/A</b> ⚠️1 Legacy row" in text
        assert f"#{pid}:" in text
    finally:
        reset_locale(token)
