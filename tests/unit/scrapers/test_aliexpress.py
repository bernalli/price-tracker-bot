"""Unit tests for AliexpressScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.core.exceptions import HTTPBlockStatus
from price_tracker.scrapers.aliexpress import AliexpressScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_aliexpress_handles_com_domain() -> None:
    scraper = AliexpressScraper()
    assert scraper.can_handle("https://www.aliexpress.com/item/1005001234567890.html")


def test_aliexpress_handles_it_domain() -> None:
    scraper = AliexpressScraper()
    assert scraper.can_handle("https://www.aliexpress.it/item/1005001234567890.html")


def test_aliexpress_handles_ru_domain() -> None:
    scraper = AliexpressScraper()
    assert scraper.can_handle("https://www.aliexpress.ru/item/1005001234567890.html")


def test_aliexpress_rejects_unrelated() -> None:
    scraper = AliexpressScraper()
    assert not scraper.can_handle("https://www.amazon.com/dp/B01")


def test_aliexpress_priority_50() -> None:
    assert AliexpressScraper.priority == 50


@pytest.mark.asyncio
async def test_aliexpress_extracts_price_from_runparams(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("aliexpress/sample_product.html")
    scraper = AliexpressScraper()
    url = "https://www.aliexpress.com/item/1005001234567890.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price == Decimal("12.99")
    assert info.currency == "USD"
    assert info.name


@pytest.mark.asyncio
async def test_aliexpress_falls_back_to_dom(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("aliexpress/sample_dom_only.html")
    scraper = AliexpressScraper()
    url = "https://www.aliexpress.com/item/1005009999999999.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price == Decimal("9.50")
    assert info.currency == "EUR"


@pytest.mark.asyncio
async def test_aliexpress_returns_error_on_missing_data() -> None:
    scraper = AliexpressScraper()
    url = "https://www.aliexpress.com/item/0.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None


def _no_wait(self: object, retry_state: object) -> float:  # noqa: ARG001
    """Zero tenacity backoff so retry tests run instantly."""
    return 0.0


@pytest.mark.asyncio
async def test_aliexpress_retries_transient_503_then_succeeds(
    load_fixture: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient 5xx must be retried inside the @with_retry scope (bug #55)."""
    monkeypatch.setattr("tenacity.wait.wait_random_exponential.__call__", _no_wait)
    html = load_fixture("aliexpress/sample_product.html")
    scraper = AliexpressScraper()
    url = "https://www.aliexpress.com/item/1005005550000001.html"
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url)
        route.mock(
            side_effect=[
                httpx.Response(503, text="temporary upstream error"),
                httpx.Response(200, text=html),
            ]
        )
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert route.call_count == 2
    assert info.error is None
    assert info.price == Decimal("12.99")


@pytest.mark.asyncio
async def test_aliexpress_raises_block_event_on_403() -> None:
    """A hard 403 must raise HTTPBlockStatus (quarantine), not ProductInfo(error=...)."""
    scraper = AliexpressScraper()
    url = "https://www.aliexpress.com/item/1005005550000002.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(403, text="<html><body>blocked</body></html>")
        async with httpx.AsyncClient() as client:
            with pytest.raises(HTTPBlockStatus) as exc:
                await scraper.scrape(url, client)
    assert exc.value.status == 403
