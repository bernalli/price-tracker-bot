"""Amazon must classify 404/410 as ListingGone and 403/429 as a block — on BOTH
the primary fetch (``_fetch_amazon_html``) and the fresh-client fallback
(``_fetch_with_fresh_client``) — instead of collapsing every non-2xx status
into a generic ``ProductInfo(error=...)`` (bug reported 2026-09-28).

Mirrors the schema already proven in ``shopify.py``: ``detect_block_event``
then ``detect_listing_gone`` BEFORE ``raise_for_status``, on every fetch path.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from price_tracker.core.exceptions import HTTPBlockStatus, ListingGone
from price_tracker.scrapers.amazon import _fetch_amazon_html, _fetch_with_fresh_client

pytestmark = pytest.mark.asyncio

_URL = "https://www.amazon.it/dp/GONE001"


# ── primary fetch path (_fetch_amazon_html) ───────────────────────


@pytest.mark.parametrize("status", [404, 410])
async def test_primary_fetch_raises_listing_gone(status: int) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(_URL).respond(status)
        async with httpx.AsyncClient() as client:
            with pytest.raises(ListingGone) as exc:
                await _fetch_amazon_html(_URL, client)
    assert exc.value.status == status


@pytest.mark.parametrize("status", [403, 429])
async def test_primary_fetch_raises_block(status: int) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(_URL).respond(status, text="blocked")
        async with httpx.AsyncClient() as client:
            with pytest.raises(HTTPBlockStatus) as exc:
                await _fetch_amazon_html(_URL, client)
    assert exc.value.status == status


# ── fresh-client fetch path (_fetch_with_fresh_client) ─────────────


@pytest.mark.parametrize("status", [404, 410])
async def test_fresh_fetch_raises_listing_gone(status: int) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(_URL).respond(status)
        with pytest.raises(ListingGone) as exc:
            await _fetch_with_fresh_client(_URL)
    assert exc.value.status == status


@pytest.mark.parametrize("status", [403, 429])
async def test_fresh_fetch_raises_block(status: int) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(_URL).respond(status, text="blocked")
        with pytest.raises(HTTPBlockStatus) as exc:
            await _fetch_with_fresh_client(_URL)
    assert exc.value.status == status
