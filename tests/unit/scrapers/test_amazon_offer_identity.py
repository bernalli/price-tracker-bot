"""A tracked Product cannot authorize a contradictory child Offer identity."""

import json
from decimal import Decimal

import pytest

from tests.unit.scrapers.test_amazon_positive_stock import ASIN, URL, scrape


@pytest.mark.parametrize(
    ("identity", "accepted"),
    [
        ({"asin": "B0FOREIGN00"}, False),
        ({"itemOffered": {"@type": "Product", "asin": "B0FOREIGN00"}}, False),
        (
            {"itemOffered": {"@type": "Product", "url": "https://www.amazon.it/dp/B0FOREIGN00"}},
            False,
        ),
        ({"asin": ASIN}, True),
        ({"itemOffered": {"@type": "Product", "asin": ASIN}}, True),
        ({"itemOffered": {"@type": "Product", "url": URL}}, True),
        ({"itemOffered": {"@id": "#product"}}, True),
    ],
    ids=["foreign-offer", "foreign-item", "foreign-url", "own-offer", "own-item", "own-url", "ref"],
)
async def test_jsonld_stock_respects_the_offer_identity(
    monkeypatch: pytest.MonkeyPatch, identity: dict[str, object], accepted: bool
) -> None:
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "asin": ASIN,
        "url": URL,
        "offers": {
            "@type": "Offer",
            **identity,
            "price": "29.99",
            "priceCurrency": "EUR",
            "availability": "https://schema.org/InStock",
        },
    }
    page = (
        f'<link rel="canonical" href="{URL}">'
        '<h1 id="productTitle">Synthetic item</h1>'
        f'<script type="application/ld+json">{json.dumps(product)}</script>'
    )
    info = await scrape(monkeypatch, page)
    assert info.price == (Decimal("29.99") if accepted else None)
    assert info.stock_evidence is accepted
