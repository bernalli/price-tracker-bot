"""Fetch-level properties of the Amazon scraper: block, gone and retry paths."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx

from price_tracker.core.exceptions import (
    BlockEvent,
    HTTPBlockStatus,
    ListingGone,
    WAFBlocked,
)
from price_tracker.scrapers import amazon as amazon_module
from price_tracker.scrapers.amazon import (
    AmazonScraper,
    _fetch_amazon_html,
    _fetch_with_fresh_client,
)

pytestmark = pytest.mark.asyncio

_URL = "https://www.amazon.it/dp/REV001"
_WAF_BODY = "<html><head><title>Just a moment...</title></head><body></body></html>"
_STUB_OK = (
    "<html><body><h1 id='productTitle'>Stub</h1><div id='corePrice_desktop'>"
    "<span class='priceToPay'><span class='a-offscreen'>19,90&euro;</span></span>"
    "</div></body></html>"
)


async def _none(url: str) -> str | None:  # noqa: ARG001
    return None


async def _must_not_run(url: str) -> str | None:  # noqa: ARG001
    raise AssertionError("fallback fetch must not run")


# -- WAF fingerprint inside a 200, per fetch path ----------------------------


async def test_primary_fetch_raises_waf_on_200_body() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(_URL).respond(200, text=_WAF_BODY)
        async with httpx.AsyncClient() as client:
            with pytest.raises(WAFBlocked):
                await _fetch_amazon_html(_URL, client)


async def test_fresh_fetch_raises_waf_on_200_body() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(_URL).respond(200, text=_WAF_BODY)
        with pytest.raises(WAFBlocked):
            await _fetch_with_fresh_client(_URL)


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
    monkeypatch.setattr(amazon_module, "_fetch_with_fresh_client", _must_not_run)
    monkeypatch.setattr(amazon_module, "_fetch_via_curl_cffi", _must_not_run)
    monkeypatch.setattr(amazon_module, "_fetch_via_scrapling", _must_not_run)
    with respx.mock(assert_all_called=False) as router:
        router.get(_URL).respond(status)
        async with httpx.AsyncClient() as client:
            with pytest.raises(ListingGone) as exc:
                await AmazonScraper().scrape(_URL, client)
    assert exc.value.status == status


# -- fresh enrichment retry must not discard the primary page -----------------


@pytest.mark.parametrize(
    "boom",
    [
        HTTPBlockStatus(status=429, url=_URL),
        WAFBlocked(provider="cloudflare", url=_URL),
        ListingGone(status=404, url=_URL),
    ],
    ids=["http-block", "waf", "gone"],
)
async def test_scrape_keeps_primary_html_when_fresh_retry_is_blocked(
    boom: BlockEvent | ListingGone, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _raise(url: str) -> str | None:  # noqa: ARG001
        raise boom

    monkeypatch.setattr(amazon_module, "_fetch_with_fresh_client", _raise)
    monkeypatch.setattr(amazon_module, "_fetch_via_curl_cffi", _must_not_run)
    monkeypatch.setattr(amazon_module, "_fetch_via_scrapling", _must_not_run)
    with respx.mock(assert_all_called=False) as router:
        router.get(_URL).respond(200, text=_STUB_OK)
        async with httpx.AsyncClient() as client:
            info = await AmazonScraper().scrape(_URL, client)
    assert info.price == Decimal("19.90")


# -- not-well-formed inputs ----------------------------------------------------


async def test_scrape_empty_body_is_error_not_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(amazon_module, "_fetch_with_fresh_client", _none)
    with respx.mock(assert_all_called=False) as router:
        router.get(_URL).respond(200, text="")
        async with httpx.AsyncClient() as client:
            info = await AmazonScraper().scrape(_URL, client)
    assert info.price is None
    assert info.error is not None


async def test_scrape_unexpected_4xx_is_error_not_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(amazon_module, "_fetch_with_fresh_client", _none)
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
    monkeypatch.setattr(amazon_module, "_fetch_with_fresh_client", _must_not_run)
    monkeypatch.setattr(amazon_module, "_fetch_via_curl_cffi", _must_not_run)
    monkeypatch.setattr(amazon_module, "_fetch_via_scrapling", _must_not_run)
    async with httpx.AsyncClient() as client:
        info = await AmazonScraper().scrape(_URL, client)
    assert info.price is None
    assert info.error is not None


async def test_fresh_fetch_network_error_returns_none() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(_URL).mock(side_effect=httpx.ConnectError("down"))
        assert await _fetch_with_fresh_client(_URL) is None


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
