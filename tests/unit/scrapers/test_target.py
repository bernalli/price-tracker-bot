"""Unit tests for TargetScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers.target import TargetScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_target_handles_target_domain() -> None:
    scraper = TargetScraper()
    assert scraper.can_handle("https://www.target.com/p/-/A-1234")


def test_target_rejects_unrelated() -> None:
    scraper = TargetScraper()
    assert not scraper.can_handle("https://www.amazon.com/dp/B01")


def test_target_priority_50() -> None:
    assert TargetScraper.priority == 50


@pytest.mark.asyncio
async def test_target_extracts_price_from_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("target/sample_product.html")
    scraper = TargetScraper()
    url = "https://www.target.com/p/-/A-1234"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency in ("USD", "EUR", "GBP", "CAD")
    assert info.name


@pytest.mark.asyncio
async def test_target_returns_error_on_missing_data() -> None:
    scraper = TargetScraper()
    url = "https://www.target.com/p/-/A-9999"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
