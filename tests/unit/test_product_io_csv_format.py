"""`/importa` reads the delimiter and validates the threshold of every row.

A file separated by ';' (the default of spreadsheet programs in many locales)
was read with ',' as the delimiter, so no row had a "URL" column and the import
reported 0 products without saying why. The "Soglia" column was split on ':'
and stored as it came, so a percentage of 150 or an unknown threshold type
reached the database.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.product_io import cmd_import

if TYPE_CHECKING:
    import io

URL = "https://example.com/products/widget"


def _context(csv_body: str) -> tuple[MagicMock, MagicMock, AsyncMock]:
    db = AsyncMock()
    db.is_user_allowed = AsyncMock(return_value=True)
    db.get_product_by_url_for_user = AsyncMock(return_value=None)
    db.add_product = AsyncMock(return_value=1)

    result = MagicMock()
    result.price = Decimal("19.99")
    result.name = "Widget"
    product_scraper = MagicMock()
    product_scraper.scrape = AsyncMock(return_value=result)
    scraper = MagicMock()
    scraper.resolve = MagicMock(return_value=product_scraper)

    context = MagicMock()
    context.bot_data = {"db": db, "http_client": MagicMock(), "scraper": scraper}

    uploaded = MagicMock()

    async def download_to_memory(buf: io.BytesIO) -> None:
        buf.write(csv_body.encode("utf-8"))

    uploaded.download_to_memory = AsyncMock(side_effect=download_to_memory)
    context.bot.get_file = AsyncMock(return_value=uploaded)

    update = MagicMock()
    update.effective_user.id = 1
    update.effective_user.language_code = "en"
    document = MagicMock()
    document.file_name = "products.csv"
    document.file_id = "file-id"
    update.message.document = document
    progress = MagicMock(edit_text=AsyncMock())
    update.message.reply_text = AsyncMock(return_value=progress)

    return update, context, db


def _replies(update: MagicMock) -> list[str]:
    texts = [str(c.args[0]) for c in update.message.reply_text.await_args_list]
    progress = update.message.reply_text.return_value
    texts += [str(c.args[0]) for c in progress.edit_text.await_args_list]
    return texts


# ── delimiter ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("delimiter", [",", ";"])
async def test_csv_import_reads_comma_and_semicolon_files(delimiter: str) -> None:
    body = delimiter.join(["URL", "Nome", "Soglia"]) + "\n"
    body += delimiter.join([URL, "Widget", "percentage:10"]) + "\n"
    update, context, db = _context(body)

    await cmd_import(update, context)

    db.add_product.assert_awaited_once()


async def test_csv_import_without_a_url_column_answers_with_an_error() -> None:
    update, context, db = _context(f"Link\tNome\n{URL}\tWidget\n")

    await cmd_import(update, context)

    db.add_product.assert_not_awaited()
    assert any("URL" in text and "❌" in text for text in _replies(update))


# ── threshold ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("soglia", "stored"),
    [
        ("percentage:10", ("percentage", Decimal(10))),
        ("absolute:50.00", ("absolute", Decimal("50.00"))),
        ("any_drop:0", ("any_drop", Decimal(0))),
    ],
)
async def test_csv_import_stores_a_valid_threshold(
    soglia: str, stored: tuple[str, Decimal]
) -> None:
    update, context, db = _context(f"URL,Soglia\n{URL},{soglia}\n")

    await cmd_import(update, context)

    kwargs = db.add_product.await_args.kwargs
    assert (kwargs["threshold_type"], kwargs["threshold_value"]) == stored


@pytest.mark.parametrize(
    "soglia",
    [
        "percentage:150",
        "percentage:0",
        "percentage:-5",
        "percentage:abc",
        "absolute:-5",
        "absolute:0",
        "absolute:1e3",
        "bogus:10",
        "percentage",
        "10",
    ],
)
async def test_csv_import_rejects_an_invalid_threshold(soglia: str) -> None:
    update, context, db = _context(f"URL,Soglia\n{URL},{soglia}\n")

    await cmd_import(update, context)

    db.add_product.assert_not_awaited()
    assert any("❌" in text for text in _replies(update))
