"""Generic/Shopify must surface WAF/CAPTCHA challenge bodies as BlockEvents (#7).

Cloudflare et al. return a 200 challenge page ("Just a moment..."); these
scrapers parsed it as a normal page instead of signalling a block, so the domain
was never quarantined.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from price_tracker.core.exceptions import BlockEvent, HTTPBlockStatus, WAFBlocked
from price_tracker.scrapers import generic as generic_module
from price_tracker.scrapers.generic import GenericScraper
from price_tracker.scrapers.shopify import ShopifyScraper

_WAF_BODY = "<html><head><title>Just a moment...</title></head><body>x</body></html>" + ("x" * 6000)


async def test_generic_raises_waf_on_challenge_body(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _waf(url: str) -> str:  # noqa: ARG001
        return _WAF_BODY

    monkeypatch.setattr(generic_module, "_fetch_with_curl_cffi", _waf)
    async with httpx.AsyncClient() as client:
        with pytest.raises(WAFBlocked):
            await GenericScraper().scrape("https://shop.example/p/1", client)


async def test_shopify_fetch_html_raises_waf_on_challenge_body() -> None:
    url = "https://shop.example/products/widget"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=_WAF_BODY)
        async with httpx.AsyncClient() as client:
            with pytest.raises(WAFBlocked):
                await ShopifyScraper._fetch_html(url, client)


# ── #16: HTTP 403/429 must surface as BlockEvent, not generic error ──


async def _no_curl(url: str) -> str | None:  # noqa: ARG001
    return None


async def test_fetch_generic_html_raises_block_on_403_cloudflare() -> None:
    """httpx 403 with a Cloudflare challenge body → HTTPBlockStatus, not HTTPStatusError."""
    url = "https://shop.example/p/403"
    body = "<html><head><title>Just a moment...</title></head><body></body></html>"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(403, text=body)
        async with httpx.AsyncClient() as client:
            with pytest.raises(HTTPBlockStatus):
                await generic_module._fetch_generic_html(url, client)


async def test_fetch_generic_html_raises_block_on_429() -> None:
    """httpx 429 → HTTPBlockStatus before raise_for_status (no retry of blocks)."""
    url = "https://shop.example/p/429"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(429, text="rate limited")
        async with httpx.AsyncClient() as client:
            with pytest.raises(HTTPBlockStatus):
                await generic_module._fetch_generic_html(url, client)


async def test_generic_scrape_propagates_block_on_httpx_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scrape() must let the BlockEvent out so the scheduler quarantines the domain."""
    monkeypatch.setattr(generic_module, "_fetch_with_curl_cffi", _no_curl)
    url = "https://shop.example/p/blocked"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(403, text="<html>blocked</html>")
        async with httpx.AsyncClient() as client:
            with pytest.raises(BlockEvent):
                await GenericScraper().scrape(url, client)


class _Blocked403Session:
    """Fake curl_cffi AsyncSession whose .get() returns an HTTP 403 response."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _Blocked403Session:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def get(self, *args: object, **kwargs: object) -> object:
        class _Resp:
            status_code = 403
            text = "<html>blocked</html>"

        return _Resp()


async def test_generic_curl_403_surfaces_block_when_httpx_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """curl_cffi 403 must not be discarded: when httpx also fails → BlockEvent."""
    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _Blocked403Session)

    async def _httpx_down(url: str, client: httpx.AsyncClient) -> str:  # noqa: ARG001
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(generic_module, "_fetch_generic_html", _httpx_down)
    async with httpx.AsyncClient() as client:
        with pytest.raises(HTTPBlockStatus):
            await GenericScraper().scrape("https://shop.example/p/curl403", client)


async def test_generic_curl_403_falls_back_to_httpx_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """curl_cffi 403 but httpx succeeds → normal ProductInfo, no quarantine."""
    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _Blocked403Session)
    url = "https://shop.example/p/curl403-ok"
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type": "Product", "name": "Widget",'
        ' "offers": {"@type": "Offer", "price": "29.99", "priceCurrency": "EUR"}}'
        "</script></head><body>Widget</body></html>"
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await GenericScraper().scrape(url, client)
    assert info.error is None
    assert info.price is not None
