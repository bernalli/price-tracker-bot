"""Generic/Shopify must surface WAF/CAPTCHA challenge bodies as BlockEvents (#7).

Cloudflare et al. return a 200 challenge page ("Just a moment..."); these
scrapers parsed it as a normal page instead of signalling a block, so the domain
was never quarantined.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from price_tracker.core.exceptions import WAFBlocked
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
