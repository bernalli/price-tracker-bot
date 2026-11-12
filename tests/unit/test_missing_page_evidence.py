"""A store's not-found document must not invent an observed HTTP status."""

from unittest.mock import AsyncMock

import httpx
import pytest

from price_tracker.core.exceptions import ListingGone
from price_tracker.core.scheduler_common import _failure_reason
from price_tracker.scrapers import amazon
from price_tracker.scrapers.amazon import AmazonScraper


def test_document_only_missing_page_does_not_claim_http_404() -> None:
    error = ListingGone(status=404, url="https://example.com/item", http_status_observed=False)
    reason, detail = _failure_reason(error)
    assert reason == "listing_gone"
    assert "HTTP 404" not in detail
    assert "HTTP 404" not in str(error)
    assert "missing" in detail


@pytest.mark.parametrize("status", [404, 410])
def test_actual_missing_http_status_is_preserved(status: int) -> None:
    reason, detail = _failure_reason(ListingGone(status=status))
    assert reason == "listing_gone"
    assert detail == f"HTTP {status}"


@pytest.mark.asyncio
async def test_amazon_not_found_document_does_not_invent_http_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = '<a href="/?ref=cs_404_link">Home</a>'
    monkeypatch.setattr(amazon, "_fetch_amazon_page", AsyncMock(return_value=page))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ListingGone) as raised:
            await AmazonScraper().scrape("https://www.amazon.it/dp/B012345678", client)
    reason, detail = _failure_reason(raised.value)
    assert reason == "listing_gone"
    assert "HTTP" not in detail
