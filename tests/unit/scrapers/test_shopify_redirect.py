"""Shopify JSON API must reject a redirect that lands on a different product (#11).

A dead /products/<slug>.json can 301 to another product's JSON; the JSON path
followed redirects without checking the handle, so it captured a DIFFERENT
product's price/name. The HTML path already guards via _is_product_path.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import respx

from price_tracker.scrapers.shopify import ShopifyScraper


async def test_json_api_rejects_redirect_to_other_product() -> None:
    scraper = ShopifyScraper()
    json_url = "https://shop.example/products/dead-slug.json"
    body = {
        "product": {
            "handle": "totally-different-product",
            "title": "Other Product",
            "variants": [{"price": "99.00"}],
        }
    }
    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(200, json=body)
        async with httpx.AsyncClient() as client:
            result = await scraper._try_json_api(json_url, client)
    assert result is None  # handle mismatch → must not capture the wrong product


async def test_json_api_accepts_matching_handle() -> None:
    scraper = ShopifyScraper()
    json_url = "https://shop.example/products/real-slug.json"
    body = {
        "product": {
            "handle": "real-slug",
            "title": "Real Product",
            "variants": [{"price": "50.00"}],
        }
    }
    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(200, json=body)
        async with httpx.AsyncClient() as client:
            result = await scraper._try_json_api(json_url, client)
    assert result is not None
    assert result.price == Decimal("50.00")
