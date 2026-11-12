"""A secondary scraper must not invent a sellable price, currency or stock."""

import importlib
import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from price_tracker.core.scraper_base import ProductInfo

STORES = [
    ("zalando", "ZalandoScraper", "_fetch_zalando_html", "https://www.zalando.co.uk/item"),
    ("apple_store", "AppleStoreScraper", "_fetch_apple_html", "https://www.apple.com/uk/shop/item"),
    ("etsy", "EtsyScraper", "_fetch_etsy_html", "https://www.etsy.com/listing/1"),
    (
        "google_store",
        "GoogleStoreScraper",
        "_fetch_google_store_html",
        "https://store.google.com/item",
    ),
    ("target", "TargetScraper", "_fetch_target_html", "https://www.target.com/p/item"),
    ("walmart", "WalmartScraper", "_fetch_walmart_html", "https://www.walmart.com/ip/item"),
    ("bestbuy", "BestbuyScraper", "_fetch_bestbuy_html", "https://www.bestbuy.com/site/item"),
    ("newegg", "NeweggScraper", "_fetch_newegg_html", "https://www.newegg.com/p/item"),
    ("wayfair", "WayfairScraper", "_fetch_wayfair_html", "https://www.wayfair.com/item"),
    ("mediamarkt", "MediamarktScraper", "_fetch_mediamarkt_html", "https://www.mediamarkt.de/item"),
    ("otto", "OttoScraper", "_fetch_otto_html", "https://www.otto.de/item"),
    ("ebay", "EbayScraper", "_fetch_ebay_html", "https://www.ebay.com/itm/1"),
]


async def scrape(store: tuple[str, str, str, str], html: str) -> ProductInfo:
    module, cls, fetch, url = store
    mod = importlib.import_module("price_tracker.scrapers." + module)
    with patch.object(mod, fetch, new=AsyncMock(return_value=html)):
        async with httpx.AsyncClient() as client:
            result = await getattr(mod, cls)().scrape(url, client)
            assert isinstance(result, ProductInfo)
            return result


def product(offers: object) -> str:
    return (
        '<script type="application/ld+json">'
        + json.dumps({"@type": "Product", "name": "Item", "offers": offers})
        + "</script>"
    )


@pytest.mark.parametrize("store", STORES, ids=lambda s: s[0])
async def test_explicit_sold_out_offer_never_reports_available(store):
    result = await scrape(
        store,
        product(
            {
                "price": "100.00",
                "priceCurrency": "GBP",
                "availability": "https://schema.org/OutOfStock",
            }
        ),
    )
    assert result.available is False


@pytest.mark.parametrize("store", STORES, ids=lambda s: s[0])
async def test_sold_out_without_price_survives_fallback(store):
    result = await scrape(store, product({"availability": "https://schema.org/OutOfStock"}))
    assert result.available is False
    assert result.price is None


@pytest.mark.parametrize(
    "offers",
    [
        {
            "@type": "AggregateOffer",
            "lowPrice": "10.00",
            "highPrice": "100.00",
            "priceCurrency": "USD",
        },
        [{"price": "10.00", "priceCurrency": "USD", "description": "Monthly installment"}],
    ],
)
async def test_ebay_range_and_installments_are_not_purchase_prices(offers):
    result = await scrape(STORES[-1], product(offers))
    assert result.price is None


async def test_zalando_uk_missing_offer_currency_is_not_silently_eur():
    result = await scrape(STORES[0], product({"price": "100.00"}))
    assert result.currency != "EUR" or result.price is None


async def test_apple_canada_currency_is_not_read_as_usd():
    store = (
        "apple_store",
        "AppleStoreScraper",
        "_fetch_apple_html",
        "https://www.apple.com/ca/shop/item",
    )
    result = await scrape(
        store, '<title>Item</title><div class="rc-prices-fullprice">CA$ 99.99</div>'
    )
    assert result.currency != "USD" or result.price is None


async def test_aliexpress_sold_out_without_price_is_not_missed():
    from price_tracker.scrapers.aliexpress import AliexpressScraper

    url = "https://www.aliexpress.com/item/1.html"
    response = httpx.Response(
        200,
        text=product({"availability": "https://schema.org/OutOfStock"}),
        request=httpx.Request("GET", url),
    )
    with patch(
        "price_tracker.scrapers.aliexpress._fetch_aliexpress_html",
        new=AsyncMock(return_value=response),
    ):
        async with httpx.AsyncClient() as client:
            result = await AliexpressScraper().scrape(url, client)
    assert result.available is False
    assert result.price is None


async def test_aliexpress_aggregate_minimum_is_not_purchase_price():
    from price_tracker.scrapers.aliexpress import AliexpressScraper

    url = "https://www.aliexpress.com/item/1.html"
    response = httpx.Response(
        200,
        text=product(
            {
                "@type": "AggregateOffer",
                "lowPrice": "10",
                "highPrice": "100",
                "priceCurrency": "USD",
            }
        ),
        request=httpx.Request("GET", url),
    )
    with patch(
        "price_tracker.scrapers.aliexpress._fetch_aliexpress_html",
        new=AsyncMock(return_value=response),
    ):
        async with httpx.AsyncClient() as client:
            result = await AliexpressScraper().scrape(url, client)
    assert result.price is None


async def test_aliexpress_does_not_override_sold_out_offer_with_runparams_minimum():
    from price_tracker.scrapers.aliexpress import AliexpressScraper

    url = "https://www.aliexpress.com/item/1.html"
    params = {
        "data": {
            "priceComponent": {
                "discountedPrice": {"minAmount": {"value": "10", "currency": "USD"}}
            },
            "titleModule": {"subject": "Item"},
        }
    }
    html = (
        "<script>window.runParams = "
        + json.dumps(params)
        + ";</script>"
        + product({"price": "100", "priceCurrency": "USD", "availability": "OutOfStock"})
    )
    response = httpx.Response(200, text=html, request=httpx.Request("GET", url))
    with patch(
        "price_tracker.scrapers.aliexpress._fetch_aliexpress_html",
        new=AsyncMock(return_value=response),
    ):
        async with httpx.AsyncClient() as client:
            result = await AliexpressScraper().scrape(url, client)
    assert result.available is False


async def test_ebay_skips_installments_before_eligible_full_price():
    offers = [
        {"price": "10.00", "priceCurrency": "USD", "description": "Monthly installment"},
        {"price": "100.00", "priceCurrency": "USD", "availability": "InStock"},
    ]
    result = await scrape(STORES[-1], product(offers))
    assert result.price == Decimal("100.00")
    assert result.currency == "USD"
    assert result.available is True


@pytest.mark.parametrize("store", STORES, ids=lambda s: s[0])
async def test_conflicting_offer_currencies_cannot_fall_back_to_other_price(store):
    offers = [
        {"price": "20.00", "priceCurrency": "EUR"},
        {"price": "30.00", "priceCurrency": "USD"},
    ]
    result = await scrape(store, product(offers))
    assert result.price is None
    assert result.error == "currency_ambiguous"
