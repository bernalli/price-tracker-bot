"""Fetch-level properties of the Amazon scraper: block, gone and retry paths."""

from __future__ import annotations

import httpx
import pytest
import respx

from price_tracker.core.exceptions import (
    HTTPBlockStatus,
    ListingGone,
    WAFBlocked,
)
from price_tracker.scrapers import amazon as amazon_module
from price_tracker.scrapers.amazon import (
    AmazonScraper,
    _fetch_amazon_html,
)

pytestmark = pytest.mark.asyncio

_URL = "https://www.amazon.it/dp/REV001"
_WAF_BODY = "<html><head><title>Just a moment...</title></head><body></body></html>"
_STUB_OK = (
    "<html><body><h1 id='productTitle'>Stub</h1><div id='corePrice_desktop'>"
    "<span class='priceToPay'><span class='a-offscreen'>19,90&euro;</span></span>"
    "</div></body></html>"
)


# -- WAF fingerprint inside a 200, per fetch path ----------------------------


async def test_primary_fetch_raises_waf_on_200_body() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(_URL).respond(200, text=_WAF_BODY)
        async with httpx.AsyncClient() as client:
            with pytest.raises(WAFBlocked):
                await _fetch_amazon_html(_URL, client)


# -- 429 is surfaced after ONE request (no tenacity retry) --------------------


async def test_primary_429_is_not_retried() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(_URL).respond(429, text="slow down")
        async with httpx.AsyncClient() as client:
            with pytest.raises(HTTPBlockStatus):
                await _fetch_amazon_html(_URL, client)
    assert route.call_count == 1


# -- scrape(): 404/410 never reaches the block fallbacks ----------------------


@pytest.mark.parametrize("status", [404, 410])
async def test_scrape_gone_skips_every_fallback(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(_URL).respond(status)
        async with httpx.AsyncClient() as client:
            with pytest.raises(ListingGone) as exc:
                await AmazonScraper().scrape(_URL, client)
    assert exc.value.status == status


# -- not-well-formed inputs ----------------------------------------------------


async def test_scrape_empty_body_is_error_not_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(_URL).respond(200, text="")
        async with httpx.AsyncClient() as client:
            info = await AmazonScraper().scrape(_URL, client)
    assert info.price is None
    assert info.error is not None


async def test_scrape_unexpected_4xx_is_error_not_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(_URL).respond(418, text="teapot")
        async with httpx.AsyncClient() as client:
            info = await AmazonScraper().scrape(_URL, client)
    assert info.price is None
    assert info.error is not None


async def test_scrape_5xx_after_retries_is_error_not_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(
        url: str, client: httpx.AsyncClient, extra: dict[str, str] | None = None
    ) -> str:
        raise httpx.HTTPStatusError(
            "boom", request=httpx.Request("GET", url), response=httpx.Response(503)
        )

    monkeypatch.setattr(amazon_module, "_fetch_amazon_html", _boom)
    async with httpx.AsyncClient() as client:
        info = await AmazonScraper().scrape(_URL, client)
    assert info.price is None
    assert info.error is not None


async def test_primary_redirect_to_404_is_listing_gone() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(_URL).respond(301, headers={"Location": _URL + "x"})
        router.get(_URL + "x").respond(404)
        async with httpx.AsyncClient() as client:
            with pytest.raises(ListingGone):
                await _fetch_amazon_html(_URL, client)


async def test_primary_redirect_to_waf_page_is_block() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(_URL).respond(302, headers={"Location": _URL + "x"})
        router.get(_URL + "x").respond(200, text=_WAF_BODY)
        async with httpx.AsyncClient() as client:
            with pytest.raises(WAFBlocked):
                await _fetch_amazon_html(_URL, client)


async def test_small_page_is_fetched_once() -> None:
    page = "<html><body>" + "x" * (1000 - len("<html><body></body></html>")) + "</body></html>"
    assert len(page) == 1000
    with respx.mock(assert_all_called=False) as router:
        route = router.get(_URL).respond(200, text=page)
        async with httpx.AsyncClient() as client:
            html = await amazon_module._fetch_amazon_page(_URL, client)
    assert route.call_count == 1
    assert html == page
