"""JSON-LD ``@graph`` containers must be unwrapped (#56).

Sites emitting ``{"@context": ..., "@graph": [..., Product, ...]}`` (Yoast/
WordPress-style) made the Product invisible to scrapers that only handled
top-level dict/list payloads, forcing a DOM/title fallback.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from bs4 import BeautifulSoup

from price_tracker.core.scraper_base import unwrap_jsonld_graph
from price_tracker.scrapers.apple_store import AppleStoreScraper
from price_tracker.scrapers.google_store import GoogleStoreScraper
from price_tracker.scrapers.zalando import ZalandoScraper

_GRAPH_HTML = (
    '<html><head><script type="application/ld+json">'
    '{"@context": "https://schema.org", "@graph": ['
    '{"@type": "BreadcrumbList", "itemListElement": []},'
    '{"@type": "Product", "name": "Graph Phone", "offers":'
    ' {"@type": "Offer", "price": "499.00", "priceCurrency": "EUR"}}]}'
    "</script></head><body></body></html>"
)


@pytest.mark.parametrize(
    "scraper_cls",
    [ZalandoScraper, AppleStoreScraper, GoogleStoreScraper],
    ids=lambda c: c.__name__,
)
def test_scraper_jsonld_finds_product_inside_graph(scraper_cls) -> None:
    result = scraper_cls()._try_jsonld(BeautifulSoup(_GRAPH_HTML, "lxml"))
    assert result is not None
    assert result["price"] == Decimal("499.00")
    assert result["name"] == "Graph Phone"


# ── unwrap_jsonld_graph helper ────────────────────────────────────


def test_unwrap_jsonld_graph_plain_dict() -> None:
    node = {"@type": "Product", "name": "X"}
    assert unwrap_jsonld_graph(node) == [node]


def test_unwrap_jsonld_graph_dict_with_graph() -> None:
    product = {"@type": "Product"}
    container = {"@context": "https://schema.org", "@graph": [{"@type": "WebPage"}, product]}
    items = unwrap_jsonld_graph(container)
    assert product in items
    assert {"@type": "WebPage"} in items


def test_unwrap_jsonld_graph_mixed_list_with_nested_graph() -> None:
    deep = {"@type": "Product", "name": "deep"}
    payload = [
        {"@type": "Organization"},
        "junk-string",
        42,
        {"@graph": [{"@graph": [deep]}]},
    ]
    items = unwrap_jsonld_graph(payload)
    assert deep in items
    assert {"@type": "Organization"} in items
    assert all(isinstance(i, dict) for i in items)


def test_unwrap_jsonld_graph_non_dict_payload() -> None:
    assert unwrap_jsonld_graph("nope") == []
    assert unwrap_jsonld_graph(None) == []


# ── schema.org Action wrappers ────────────────────────────────────────────


def test_unwrap_jsonld_graph_descends_into_action_object() -> None:
    """A Product carried as BuyAction.object must surface as its own node.

    MediaMarkt emits `{"@type": "BuyAction", "object": {"@type": "Product"}}`;
    scanning only top-level @type made the Product — and its price — invisible.
    """
    payload = {
        "@context": "https://schema.org/",
        "@type": "BuyAction",
        "object": {
            "@type": "Product",
            "name": "Wrapped Monitor",
            "offers": {"@type": "Offer", "price": "259", "priceCurrency": "EUR"},
        },
    }
    nodes = unwrap_jsonld_graph(payload)
    products = [n for n in nodes if "Product" in str(n.get("@type", ""))]
    assert len(products) == 1
    assert products[0]["name"] == "Wrapped Monitor"


def test_unwrap_jsonld_graph_keeps_non_action_object_property_opaque() -> None:
    """`object` is only followed on Actions, not on arbitrary nodes."""
    payload = {"@type": "Product", "object": {"@type": "Product", "name": "Nested"}}
    nodes = unwrap_jsonld_graph(payload)
    assert len(nodes) == 1
    assert nodes[0].get("name") is None
