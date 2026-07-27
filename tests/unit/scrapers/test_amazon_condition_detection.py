"""Amazon buy-box condition detection.

``_detect_condition`` concluded "new" as soon as it found *any* price inside a
core buy-box container. Amazon renders used and Warehouse offers in that very
same container, so the used branch below it was unreachable in practice and a
second-hand price was reported as the new-product price.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from price_tracker.scrapers.amazon import AmazonScraper

NEW_BUYBOX = """
<div id="corePriceDisplay_desktop_feature_div">
  <span class="a-price priceToPay"><span class="a-offscreen">464,78 €</span></span>
</div>
"""

USED_BUYBOX = """
<div id="corePriceDisplay_desktop_feature_div">
  <span class="a-price priceToPay"><span class="a-offscreen">187,95 €</span></span>
  <div class="a-row"><span class="a-text-bold">Usato - Come nuovo</span></div>
</div>
"""

WAREHOUSE_BUYBOX = """
<div id="corePrice_feature_div">
  <span class="a-price priceToPay"><span class="a-offscreen">187,95 €</span></span>
</div>
<div id="merchant-info">Venduto da Amazon Seconda mano e spedito da Amazon</div>
"""


def _condition(html: str, seller: str = "") -> str:
    return AmazonScraper()._detect_condition(BeautifulSoup(html, "lxml"), seller_name=seller)


def test_plain_buybox_price_is_new() -> None:
    assert _condition(NEW_BUYBOX) == "new"


def test_used_marker_in_buybox_is_not_new() -> None:
    """A price in the buy-box is not evidence of a new item when the box says used."""
    assert _condition(USED_BUYBOX) == "used"


def test_warehouse_seller_is_used() -> None:
    assert _condition(WAREHOUSE_BUYBOX, seller="Amazon Seconda mano") == "used"


def test_renewed_marker_is_renewed() -> None:
    html = NEW_BUYBOX.replace("</div>", "<span>Ricondizionato certificato</span></div>")
    assert _condition(html) == "renewed"


def test_used_only_buybox_still_detected() -> None:
    html = '<div id="usedOnlyBuybox"><span class="a-offscreen">99,00 €</span></div>'
    assert _condition(html) == "used"
