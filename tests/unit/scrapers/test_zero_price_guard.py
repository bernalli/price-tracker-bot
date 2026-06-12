"""Zero-price placeholders must not be accepted as a real price (#61).

Target/Walmart/Bestbuy/Newegg accepted any non-None strategy price, so a page
rendering a $0.00 DOM placeholder and/or a JSON-LD offer with price "0"
produced ProductInfo(price=0). AliExpress and the generic scraper already
guard with `> 0`; these four must do the same and fall through to the
"price not found" error path.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from price_tracker.scrapers.bestbuy import BestbuyScraper
from price_tracker.scrapers.newegg import NeweggScraper
from price_tracker.scrapers.target import TargetScraper
from price_tracker.scrapers.walmart import WalmartScraper

_ZERO_JSONLD = """
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Product", "name": "Zero Product",
 "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"}}
</script>
"""

# Per-scraper page with BOTH a zero-price DOM placeholder (scraper-specific
# selector) and a zero-price JSON-LD offer, so every strategy is exercised.
CASES = [
    pytest.param(
        TargetScraper,
        "https://www.target.com/p/-/A-0000",
        f"""<html><head><title>Zero</title>{_ZERO_JSONLD}</head><body>
        <span data-test="product-price">$0.00</span>
        </body></html>""",
        id="target",
    ),
    pytest.param(
        WalmartScraper,
        "https://www.walmart.com/ip/0",
        f"""<html><head><title>Zero</title>{_ZERO_JSONLD}</head><body>
        <div itemprop="offers" itemscope>
          <span itemprop="price" content="0.00">$0.00</span>
          <span itemprop="priceCurrency" content="USD"></span>
        </div>
        </body></html>""",
        id="walmart",
    ),
    pytest.param(
        BestbuyScraper,
        "https://www.bestbuy.com/site/x/0.p",
        f"""<html><head><title>Zero</title>{_ZERO_JSONLD}</head><body>
        <div class="priceView-customer-price"><span>$0.00</span></div>
        </body></html>""",
        id="bestbuy",
    ),
    pytest.param(
        NeweggScraper,
        "https://www.newegg.com/p/N82E0",
        f"""<html><head><title>Zero</title>{_ZERO_JSONLD}</head><body>
        <div class="product-price"><ul><li class="price-current">
        <span class="currency-symbol">$</span><strong>0</strong><sup>.00</sup>
        </li></ul></div>
        </body></html>""",
        id="newegg",
    ),
]


@pytest.mark.parametrize(("scraper_cls", "url", "html"), CASES)
async def test_zero_price_placeholder_is_rejected(scraper_cls: type, url: str, html: str) -> None:
    scraper = scraper_cls()
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
