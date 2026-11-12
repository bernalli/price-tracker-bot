"""Price, currency and stock must remain one retailer observation."""

from decimal import Decimal

import pytest

from tests.integration.test_remaining_corrected import ALL_STORES, ld, scrape

DOM = {
    "target": '<div data-test="product-price">£99</div>',
    "walmart": '<main><span itemprop="price">£99</span></main>',
    "bestbuy": '<div class="priceView-customer-price"><span>£99</span></div>',
    "newegg": (
        '<div class="product-price"><li class="price-current">'
        '<strong>99</strong><span class="currency-symbol">£</span></li></div>'
    ),
    "ebay": '<div class="x-price-primary"><span itemprop="price">£99</span></div>',
}
OWNED = [store for store in ALL_STORES if store[0] in DOM]
LOOP_STORES = [store for store in OWNED if store[0] != "ebay"]


@pytest.mark.parametrize("store", LOOP_STORES, ids=lambda store: store[0])
@pytest.mark.parametrize("named", [False, True], ids=["unnamed", "named"])
async def test_incomplete_json_quote_never_borrows_dom_currency_or_stock(store, named):
    node = {
        "@type": "Product",
        "url": store[3],
        "offers": {"price": "100", "availability": "InStock"},
    }
    if named:
        node["name"] = "Requested"
    result = await scrape(store, ld(node) + DOM[store[0]])
    assert (result.price, result.currency, result.error) == (Decimal("99"), "GBP", None)
    assert result.stock_evidence is False


@pytest.mark.parametrize("store", OWNED, ids=lambda store: store[0])
@pytest.mark.parametrize("stock", [None, "InStock", "OutOfStock"])
async def test_structured_quote_preserves_only_its_own_stock_evidence(store, stock):
    offer = {"price": "100", "priceCurrency": "GBP"}
    if stock:
        offer["availability"] = stock
    node = {"@type": "Product", "url": store[3], "offers": offer}
    result = await scrape(store, ld(node) + DOM[store[0]])
    assert result.currency == "GBP"
    assert result.stock_evidence is (stock is not None)
    assert result.available is (stock != "OutOfStock")
    if stock != "OutOfStock":
        assert result.price == Decimal("100")
        assert result.error is None


@pytest.mark.parametrize("store", OWNED, ids=lambda store: store[0])
async def test_complete_dom_quote_has_price_and_unit_but_no_stock_proof(store):
    result = await scrape(store, DOM[store[0]])
    assert (result.price, result.currency, result.error) == (Decimal("99"), "GBP", None)
    assert result.stock_evidence is False


@pytest.mark.parametrize("store", OWNED, ids=lambda store: store[0])
async def test_unpriced_stock_does_not_attach_to_a_separate_dom_quote(store):
    node = {
        "@type": "Product",
        "url": store[3],
        "offers": {"availability": "InStock", "priceCurrency": "GBP"},
    }
    result = await scrape(store, ld(node) + DOM[store[0]])
    assert (result.price, result.currency, result.error) == (Decimal("99"), "GBP", None)
    assert result.stock_evidence is False


@pytest.mark.parametrize("qualifier", ["/month", " monthly installment", "–£100"])
async def test_ebay_selector_keeps_qualifier_outside_numeric_span(qualifier):
    store = next(store for store in OWNED if store[0] == "ebay")
    html = f'<div class="x-price-primary"><span class="ux-textspans">£10</span>{qualifier}</div>'
    result = await scrape(store, html)
    assert result.price is None
    assert result.error is not None
    assert result.stock_evidence is False


@pytest.mark.parametrize("retailer", ["ebay", "newegg"])
@pytest.mark.parametrize("qualifier", ["/month", " monthly installment", "–£100"])
async def test_numeric_subnode_cannot_erase_financing_or_range_context(retailer, qualifier):
    store = next(store for store in OWNED if store[0] == retailer)
    if retailer == "ebay":
        html = (
            '<div class="x-price-primary"><span itemprop="price" content="10">'
            f"£10{qualifier}</span>"
            '<meta itemprop="priceCurrency" content="GBP"></div>'
        )
    else:
        html = (
            '<div class="product-price"><li class="price-current">'
            '<strong>10</strong><sup>.00</sup><span class="currency-symbol">£</span>'
            f"{qualifier}</li></div>"
        )
    result = await scrape(store, html)
    assert result.price is None
    assert result.error is not None
    assert result.stock_evidence is False


@pytest.mark.parametrize(
    ("retailer", "html"),
    [
        (
            "ebay",
            '<div class="x-price-primary"><span itemprop="price" content="99.95">'
            "US $99.95</span></div>",
        ),
        (
            "newegg",
            '<div class="product-price"><li class="price-current">'
            '<span class="currency-symbol">US$</span><strong>99</strong>'
            "<sup>.95</sup></li></div>",
        ),
    ],
)
async def test_valid_content_and_split_price_remain_readable(retailer, html):
    store = next(store for store in OWNED if store[0] == retailer)
    result = await scrape(store, html)
    assert (result.price, result.currency, result.error) == (Decimal("99.95"), "USD", None)
    assert result.stock_evidence is False
