"""Independent adversarial cases for product, variant, currency and stock evidence."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest

from price_tracker.core.exceptions import BlockEvent, ListingGone
from price_tracker.core.page_image import ImagePageHTML
from price_tracker.scrapers import amazon, generic, shopify
from price_tracker.scrapers.amazon import AmazonScraper
from price_tracker.scrapers.generic import GenericScraper
from price_tracker.scrapers.shopify import ShopifyScraper

URL = "https://shop.example.com/item"
SHOP = "https://shop.example.com/products/item"
AMAZON = "https://www.amazon.com/dp/B012345678"
if TYPE_CHECKING:
    from price_tracker.core.scraper_base import ProductInfo

pytestmark = pytest.mark.asyncio


def ld(payload: object) -> str:
    return '<script type="application/ld+json">' + json.dumps(payload) + "</script>"


def product(
    url: str | None = URL, *, price: str = "90", stock: str = "OutOfStock"
) -> dict[str, Any]:
    data = {
        "@type": "Product",
        "name": "Item",
        "offers": {
            "price": price,
            "priceCurrency": "EUR",
            "availability": "https://schema.org/" + stock,
        },
    }
    if url is not None:
        data["url"] = url
    return data


async def scrape_generic(monkeypatch, html: str, final: str = URL) -> ProductInfo:
    monkeypatch.setattr(
        generic, "_fetch_generic_html", AsyncMock(return_value=ImagePageHTML(html, final))
    )
    async with httpx.AsyncClient() as client:
        return await GenericScraper().scrape(URL, client)


async def scrape_amazon(monkeypatch, html: str, final: str = AMAZON) -> ProductInfo:
    monkeypatch.setattr(
        amazon, "_fetch_amazon_page", AsyncMock(return_value=ImagePageHTML(html, final))
    )
    async with httpx.AsyncClient() as client:
        return await AmazonScraper().scrape(AMAZON, client)


@pytest.mark.parametrize("kind", ["redirect", "canonical", "og", "node_id"])
async def test_generic_rejects_identity_conflicts_before_any_fallback(monkeypatch, kind):
    html = (
        '<meta property="og:price:amount" content="90">'
        '<meta property="og:price:currency" content="EUR">'
    )
    final = URL
    if kind == "redirect":
        final = "https://shop.example.com/other"
        html = ld(product(final))
    elif kind == "node_id":
        p = product(None)
        p["@id"] = "https://shop.example.com/other"
        html = ld(p)
    elif kind == "canonical":
        html += '<link rel="canonical" href="https://shop.example.com/other">'
    else:
        html += '<meta property="og:url" content="https://shop.example.com/other">'
    info = await scrape_generic(monkeypatch, html, final)
    assert info.price is None
    assert info.error


async def test_generic_conflicting_owned_nodes_are_terminal(monkeypatch):
    info = await scrape_generic(monkeypatch, ld([product(price="90"), product(price="91")]))
    assert info.price is None
    assert info.error


async def test_generic_same_product_canonical_redirect_remains_readable(monkeypatch):
    html = '<link rel="canonical" href="' + URL + '">' + ld(product(stock="InStock"))
    info = await scrape_generic(monkeypatch, html, "https://shop.example.com/renamed-item")
    assert info.price == Decimal("90")
    assert info.currency == "EUR"


@pytest.mark.parametrize(
    "identifier",
    [
        {"sku": "B087654321"},
        {"productID": "asin:B087654321"},
        {},
        {"url": [AMAZON, "https://www.amazon.com/dp/B087654321"]},
    ],
)
async def test_amazon_unowned_jsonld_cannot_override_own_buybox(monkeypatch, identifier):
    data = {
        "@type": "Product",
        "name": "Recommendation",
        **identifier,
        "offers": {"price": "40", "priceCurrency": "EUR"},
    }
    html = (
        '<h1 id="productTitle">Item</h1><span class="priceToPay">'
        '<span class="a-offscreen">EUR 120.00</span></span>' + ld(data)
    )
    info = await scrape_amazon(monkeypatch, html)
    assert info.price == Decimal("120.00")
    assert info.currency == "EUR"


async def test_amazon_redirect_home_does_not_read_card_price(monkeypatch):
    info = await scrape_amazon(
        monkeypatch,
        '<span class="priceToPay"><span class="a-offscreen">EUR 120.00</span></span>',
        "https://www.amazon.com/",
    )
    assert info.price is None


@pytest.mark.parametrize("symbol", ["$", "¥", "kr"])
async def test_amazon_split_price_keeps_ambiguous_currency_evidence(monkeypatch, symbol):
    html = (
        '<h1 id="productTitle">Item</h1><div id="corePrice_feature_div">'
        '<span class="a-price"><span class="a-price-symbol">'
        + symbol
        + '</span><span class="a-price-whole">120</span>'
        '<span class="a-price-fraction">00</span></span></div>'
    )
    info = await scrape_amazon(monkeypatch, html)
    assert info.price is None
    assert info.error == "currency_ambiguous"


async def test_amazon_split_price_retains_explicit_euro_currency(monkeypatch):
    html = (
        '<div id="corePrice_feature_div"><span class="a-price">'
        '<span class="a-price-symbol">€</span><span class="a-price-whole">120</span>'
        '<span class="a-price-fraction">00</span></span></div>'
    )
    info = await scrape_amazon(monkeypatch, html)
    assert info.price == Decimal("120.00")
    assert info.currency == "EUR"


async def test_amazon_jsonld_ambiguous_currency_is_not_numeric_success(monkeypatch):
    info = await scrape_amazon(
        monkeypatch,
        ld({"@type": "Product", "url": AMAZON, "offers": {"price": "120", "priceCurrency": "$"}}),
    )
    assert info.price is None
    assert info.error == "currency_ambiguous"


@pytest.mark.parametrize(
    "markup",
    [
        '<div class="product-price">$90.00</div>',
        '<span itemprop="price">$90.00</span>',
        '<meta property="og:price:amount" content="$90.00">',
    ],
)
async def test_generic_ambiguous_currency_is_terminal(monkeypatch, markup):
    info = await scrape_generic(monkeypatch, "<title>Item</title>" + markup)
    assert info.price is None
    assert info.error == "currency_ambiguous"


async def test_generic_unbound_jsonld_preserves_selected_stock(monkeypatch):
    info = await scrape_generic(monkeypatch, ld(product(None)))
    assert info.available is False


async def test_generic_other_offer_stock_does_not_change_selected_offer(monkeypatch):
    data = product()
    data["offers"] = [
        data["offers"],
        {"price": "10", "priceCurrency": "EUR", "availability": "https://schema.org/InStock"},
    ]
    info = await scrape_generic(monkeypatch, ld(data))
    assert info.available is False


def group() -> dict[str, Any]:
    return {
        "@type": "ProductGroup",
        "url": SHOP,
        "name": "Item",
        "hasVariant": [
            {
                "@type": "Product",
                "offers": {
                    "@type": "Offer",
                    "url": SHOP + "?variant=2",
                    "price": "90",
                    "priceCurrency": "EUR",
                    "availability": "https://schema.org/OutOfStock",
                },
            }
        ],
    }


@pytest.mark.parametrize("source", ["embedded", "group"])
async def test_shopify_html_selected_variant_stock(source):
    html = (
        ld(group())
        if source == "group"
        else '<script id="ProductJson-main">'
        + json.dumps(
            {
                "handle": "item",
                "title": "Item",
                "variants": [{"id": 2, "price": 9000, "available": False}],
            }
        )
        + "</script>"
    )
    info = ShopifyScraper()._read_product_page(html, SHOP + "?variant=2")
    assert info is not None
    assert info.available is False


@pytest.mark.parametrize("query", ["?variant=2&variant=3", "?variant=", "?variant=bad"])
async def test_shopify_group_cannot_bypass_invalid_selector(query):
    info = ShopifyScraper()._read_product_page(ld(group()), SHOP + query)
    assert info is not None
    assert info.price is None


async def test_shopify_selected_unpriced_api_variant_cannot_take_css_price(monkeypatch):
    body = {
        "product": {
            "handle": "item",
            "variants": [
                {"id": 1, "price": "90", "price_currency": "EUR", "available": True},
                {"id": 2, "price": None, "available": True},
            ],
        }
    }
    response = httpx.Response(200, json=body, request=httpx.Request("GET", SHOP + ".json"))
    monkeypatch.setattr(shopify, "_fetch_shopify_json", AsyncMock(return_value=response))
    monkeypatch.setattr(
        ShopifyScraper,
        "_fetch_html",
        AsyncMock(return_value='<span class="price-item--regular">EUR 90</span>'),
    )
    async with httpx.AsyncClient() as client:
        info = await ShopifyScraper().scrape(SHOP + "?variant=2", client)
    assert info.price is None


async def test_shopify_css_cannot_claim_selected_variant():
    info = ShopifyScraper()._read_product_page(
        '<span class="price-item--regular">EUR 90</span>', SHOP + "?variant=2"
    )
    assert info is None or info.price is None


async def test_shopify_css_ambiguous_currency_is_terminal():
    info = ShopifyScraper()._read_product_page(
        '<span class="price-item--regular">$90.00</span>', SHOP
    )
    assert info is not None
    assert info.price is None
    assert info.error == "currency_ambiguous"


async def test_shopify_currency_enrichment_ignores_other_product(monkeypatch):
    body = {
        "product": {"handle": "item", "variants": [{"id": 2, "price": "90", "available": True}]}
    }
    response = httpx.Response(200, json=body, request=httpx.Request("GET", SHOP + ".json"))
    html = '<span class="product-price">USD 90.00</span>' + ld(
        product("https://shop.example.com/products/other", price="8")
    )
    monkeypatch.setattr(shopify, "_fetch_shopify_json", AsyncMock(return_value=response))
    monkeypatch.setattr(
        ShopifyScraper, "_fetch_html", AsyncMock(return_value=ImagePageHTML(html, SHOP))
    )
    async with httpx.AsyncClient() as client:
        info = await ShopifyScraper().scrape(SHOP + "?variant=2", client)
    assert info.currency != "EUR"


async def test_shopify_json_404_challenge_stays_block(monkeypatch):
    response = httpx.Response(
        404, text='<div class="g-recaptcha"></div>', request=httpx.Request("GET", SHOP + ".json")
    )
    monkeypatch.setattr(shopify, "_fetch_shopify_json", AsyncMock(return_value=response))
    monkeypatch.setattr(
        ShopifyScraper, "_fetch_html", AsyncMock(side_effect=ListingGone(status=404, url=SHOP))
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(BlockEvent):
            await ShopifyScraper().scrape(SHOP, client)


@pytest.mark.parametrize(
    "markup",
    [
        '<div id="recommendations"><span class="product-price">EUR 90</span></div>',
        '<script>dataLayer.push({"price":90});</script>',
        '<script>var recommendation={"name":"Other","price":90};</script>',
    ],
)
async def test_generic_recommendation_or_analytics_cannot_supply_price(monkeypatch, markup):
    info = await scrape_generic(monkeypatch, "<title>Item</title>" + markup)
    assert info.price is None


async def test_generic_unpriced_out_of_stock_is_not_parse_failure(monkeypatch):
    data = product()
    data["offers"].pop("price")
    info = await scrape_generic(monkeypatch, ld(data))
    assert info.price is None
    assert info.available is False


async def test_shopify_group_unpriced_out_of_stock_is_not_parse_failure():
    data = group()
    data["hasVariant"][0]["offers"].pop("price")
    info = ShopifyScraper()._read_product_page(ld(data), SHOP + "?variant=2")
    assert info is not None
    assert info.price is None
    assert info.available is False


@pytest.mark.parametrize(
    "other_url", [SHOP + "?variant=3", "https://shop.example.com/products/other?variant=2"]
)
async def test_shopify_group_offer_list_cannot_substitute_foreign_variant(other_url):
    data = group()
    own = data["hasVariant"][0]["offers"]
    data["hasVariant"][0]["offers"] = [own, {**own, "url": other_url, "price": "120"}]
    info = ShopifyScraper()._read_product_page(ld(data), SHOP + "?variant=2")
    assert info is not None
    assert info.price is None


async def test_shopify_unrelated_javascript_cannot_supply_currency():
    info = ShopifyScraper()._read_product_page(
        '<script>var analytics={"currency":"USD"}</script><span class="product__price">90</span>',
        SHOP,
    )
    assert info is not None
    assert info.currency is None


@pytest.mark.parametrize(
    "markup",
    [
        '<div class="product-price" data-price="90">$90</div>',
        '<span itemprop="price" content="90">$90</span>',
    ],
)
async def test_generic_numeric_attribute_cannot_hide_ambiguous_visible_symbol(monkeypatch, markup):
    info = await scrape_generic(monkeypatch, markup)
    assert info.price is None
    assert info.error == "currency_ambiguous"


async def test_shopify_numeric_attribute_cannot_hide_ambiguous_visible_symbol():
    info = ShopifyScraper()._read_product_page(
        '<span class="product-price" data-product-price="90">$90</span>', SHOP
    )
    assert info is not None
    assert info.price is None
    assert info.error == "currency_ambiguous"


@pytest.mark.parametrize(
    "data",
    [
        {"name": "Item", "recommendations": [{"name": "Other", "price": 90}]},
        {"name": "Other", "url": "https://shop.example.com/other", "price": 90},
    ],
)
async def test_generic_product_assignment_requires_own_direct_price(monkeypatch, data):
    info = await scrape_generic(
        monkeypatch, "<script>var product=" + json.dumps(data) + "</script>"
    )
    assert info.price is None


@pytest.mark.parametrize(
    "currency_html",
    [
        '<meta property="og:price:currency" content="$">',
        '<meta property="og:price:currency" content="USD">'
        '<meta property="product:price:currency" content="EUR">',
    ],
)
async def test_shopify_unresolved_page_currency_cannot_leave_numeric_success(currency_html):
    info = ShopifyScraper()._read_product_page(
        currency_html + '<span class="product__price">90</span>', SHOP
    )
    assert info is not None
    assert info.price is None
    assert info.error == "currency_ambiguous"
