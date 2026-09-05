"""A CSV import must apply the same URL boundary as an interactive addition.

`/aggiungi` validates the URL with `validate_public_url` before anything resolves
or fetches it, so a user cannot point the bot at loopback, link-local or private
addresses. `/importa` reads URLs out of a file and went straight to `resolve()`
and `scrape()` — and `resolve()` hands any host with a `/products/<slug>` path to
the Shopify scraper, which fetches it. A CSV row was therefore enough to make the
bot request `http://127.0.0.1:9090/metrics` or the cloud metadata endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.product_io import cmd_import

if TYPE_CHECKING:
    import io

# Loopback and link-local are enough to prove the guard runs before resolve();
# every non-public family is already covered against validate_public_url itself
# in tests/unit/test_url_utils.py.
UNSAFE_ROWS = (
    "http://127.0.0.1:9090/products/metrics",
    "http://169.254.169.254/products/latest",
)


def _context(csv_body: str) -> tuple[MagicMock, MagicMock, AsyncMock]:
    db = AsyncMock()
    db.is_user_allowed = AsyncMock(return_value=True)
    db.get_product_by_url_for_user = AsyncMock(return_value=None)
    scraper = MagicMock()
    scraper.resolve = MagicMock(return_value=None)

    context = MagicMock()
    context.bot_data = {"db": db, "http_client": MagicMock(), "scraper": scraper}

    uploaded = MagicMock()

    async def download_to_memory(buf: io.BytesIO) -> None:
        buf.write(csv_body.encode("utf-8"))

    uploaded.download_to_memory = AsyncMock(side_effect=download_to_memory)
    context.bot.get_file = AsyncMock(return_value=uploaded)

    update = MagicMock()
    update.effective_user.id = 1
    update.effective_user.language_code = "it"
    document = MagicMock()
    document.file_name = "products.csv"
    document.file_id = "file-id"
    update.message.document = document
    update.message.reply_text = AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))

    return update, context, scraper


@pytest.mark.parametrize("url", UNSAFE_ROWS)
async def test_csv_import_rejects_non_public_urls_before_resolving(url: str) -> None:
    update, context, scraper = _context(f"URL,Nome\n{url},whatever\n")

    await cmd_import(update, context)

    scraper.resolve.assert_not_called()


async def test_csv_import_still_accepts_a_public_url() -> None:
    update, context, scraper = _context("URL,Nome\nhttps://example.com/products/x,Widget\n")

    await cmd_import(update, context)

    scraper.resolve.assert_called_once()
