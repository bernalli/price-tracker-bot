# Plugin scrapers

`price-tracker-bot` supports two ways to add scrapers without forking the repo:

1. **Drop-in directory** — place a `.py` file in `plugins/` (gitignored except for `README.md`). The registry auto-discovers it at startup.
2. **Pip-installable package** — declare a `price_tracker.scrapers` entry point in your distribution's `pyproject.toml`. Useful for sharing scrapers across projects or publishing to a private index.

Both forms must subclass `AbstractScraper` from `price_tracker.core.scraper_base`. The contract is identical to built-in scrapers (see [scrapers.md](scrapers.md)).

## Contract

Every scraper subclasses `AbstractScraper` and provides:

- `name: ClassVar[str]` — unique stable identifier. Must be set explicitly; the base class default is an empty string `""`. A blank `name` will collide with any other plugin that also omits it.
- `priority: ClassVar[int]` — resolution priority. Higher wins. Site-specific scrapers use 50; the Shopify generic uses 80; Amazon and eBay use 100. Pick a value that fits your scraper's specificity.
- `domain_patterns: ClassVar[list[re.Pattern[str]]]` — list of compiled regexes against URL netloc (`urlparse(url).netloc`).
- `min_check_interval_minutes: ClassVar[int]` — optional, default `0`. Minimum minutes between two scheduled checks of a product this scraper reads; manual checks are not limited. The Shopify scraper sets `360`; a subclass inherits it.
- `def can_handle(self, url: str) -> bool` — required abstract method. For typical domain-based matching, just `return self.matches_domain(url)` (the helper in the base class checks each `domain_patterns` regex against the URL netloc).
- `async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo` — fetch the URL with the injected `httpx.AsyncClient` (configured with public-address validation and timeouts; environment proxies are disabled) and return a populated `ProductInfo`. Never raise on network or parse errors — return `ProductInfo(error="...")` instead. Two failures are the exception and MUST be raised: `BlockEvent` when the site refuses the request (403/429, WAF, CAPTCHA), and `ListingGone` for a confirmed missing-page response (404/410, or an identified store-specific not-found document). An inconclusive redirect does not prove a missing product. When only the document identifies a missing page, use `http_status_observed=False` so no unobserved HTTP status is reported. The scheduler routes those two differently — a block quarantines the whole domain, a gone listing suspends that single product with a reason — and a returned `ProductInfo` cannot carry the distinction.

`ProductInfo` is a dataclass (see [scraper_base.py](../src/price_tracker/core/scraper_base.py)):

- `name: str | None` — product title/name
- `price: Decimal | None` — a finite, positive `Decimal` with a verified currency; never `float`
- `currency: str | None` — ISO 4217 (e.g. `"EUR"`, `"USD"`)
- `available: bool` — stock value, meaningful only when `stock_evidence` is true
- `stock_evidence: bool` — true only for interpretable stock evidence belonging to the selected product and offer. Set false when stock is unknown. The default is true for legacy plugin compatibility; managed scrapers set it explicitly.
- `seller: str | None` — third-party seller name (optional)
- `condition: str | None` — `"new"`, `"used"`, etc. (optional)
- `error: str | None` — human-readable error if the parse failed

Keep the amount, currency and stock attached to the same eligible offer. Exclude financing, reference prices and unrelated recommendations. A readable price does not establish stock; a quote without a known ISO currency is not accepted automatically.

## Minimal example

When the shared generic parser supports the page, give the store a specific route:

```python
import re
from typing import ClassVar

from price_tracker.scrapers.generic import GenericScraper


class MyShopScraper(GenericScraper):
    name: ClassVar[str] = "myshop"
    priority: ClassVar[int] = 85
    domain_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"^(www\.)?myshop\.(com|eu)$", re.IGNORECASE),
    ]

    def can_handle(self, url: str) -> bool:
        return self.matches_domain(url)
```

The inherited extraction uses the shared product identity, currency, stock and
validated transport rules. For a custom parser, use `fetch_page(url, client)`
from `price_tracker.core.fetch`, allow typed `BlockEvent` and `ListingGone` to
propagate, and construct `ProductInfo` only from the requested product's own
offer. An explicit unavailable observation may omit its price; an automatic
priced quote must have a verified currency. Keep `stock_evidence=False` when
stock is unknown, and keep exception details out of the returned user message.

## Drop-in installation

Save the file as `plugins/myshop.py` from the repository root. The registry scans this directory at startup (`core.registry.discover_dropin_scrapers`). The directory is gitignored except for `README.md`, so your file will not be accidentally committed.

```
plugins/
├── README.md        # tracked
├── myshop.py        # gitignored, auto-loaded
└── another.py       # gitignored, auto-loaded
```

Restart the bot to pick up new files. Errors during import are logged but do not stop the bot — a broken plugin is silently skipped.

## Pip-installable plugin

> **Status**: planned, not yet wired into startup. The `[project.entry-points."price_tracker.scrapers"]` group is reserved in `pyproject.toml`, but the registry does not currently call `importlib.metadata.entry_points` at startup. Until that path is wired, prefer the drop-in directory above.

When entry-point loading lands, plugin distributions will declare their scrapers like this:

```toml
[project.entry-points."price_tracker.scrapers"]
myshop = "my_plugin.scraper:MyShopScraper"
```

Track the implementation status via the project changelog and the `core.registry` module.

## Auto-discovery order

At startup the registry assembles the scraper pool from two sources, in this order:

1. **Built-in** scrapers from `src/price_tracker/scrapers/` (always loaded via `discover_builtin_scrapers`).
2. **Drop-in** plugins from `plugins/` (loaded via `discover_dropin_scrapers` if the directory exists and contains `.py` files).

Entry-point plugins are documented in the [Pip-installable plugin](#pip-installable-plugin) section above but are not yet wired into startup; they will become a third loading stage when implementation lands.

The registry sorts by `priority` (descending), preserving registration order for ties. Duplicate names are rejected: a drop-in with a built-in name does not override that built-in. Drop-in imports and constructors still run before registration, so keep them free of network and filesystem side effects. A handler for a specific domain should use priority above 80 when it must precede the Shopify-style `/products/` heuristic.

## Testing

Provide an HTML fixture and a unit test alongside your plugin:

```
tests/fixtures/myshop/sample_product.html   # captured offline
tests/unit/scrapers/test_myshop.py          # parametrized test
```

Minimal `tests/unit/scrapers/test_myshop.py`:

```python
from pathlib import Path

import httpx
import pytest
import respx

from price_tracker.core.scraper_base import ProductInfo

from my_plugin.scraper import MyShopScraper  # or `plugins.myshop` for drop-in


@pytest.mark.asyncio
async def test_myshop_parses_sample():
    fixture = Path("tests/fixtures/myshop/sample_product.html").read_text()

    with respx.mock(assert_all_called=False) as router:
        router.get("https://myshop.com/p/1").mock(return_value=httpx.Response(200, text=fixture))

        async with httpx.AsyncClient() as client:
            scraper = MyShopScraper()
            info = await scraper.scrape("https://myshop.com/p/1", client)

    assert info.error is None
    assert info.price is not None
    assert info.name
    assert info.currency == "EUR"
    assert info.available is True
```

Run with `pytest tests/unit/scrapers/test_myshop.py -v`. Coverage target: ≥80% for the plugin module (same gate as built-in scrapers — see [CONTRIBUTING.md](../CONTRIBUTING.md)).

## Best practices

- **Use the injected `client`** — it carries the bot's timeout, the public-address guard and, for store requests, the single Chrome identity. Do not create your own `httpx.AsyncClient` inside `scrape()`.
- **Return errors, do not raise** — `ProductInfo(error="...")` is logged and the domain is recorded in `scraper_health`. Raising anything other than `BlockEvent` or `ListingGone` will be caught by the scheduler but the error context is lost. Those two must be raised: swallowing them into a `ProductInfo` is what once let a blocking store suspend every product tracked on it, one message each, without ever tripping the circuit breaker.
- **Use `parse_price()`** — reads one visible price text (thousands separators, currency symbols, comma or dot decimals) and returns `Decimal | None`. A text that is not exactly one unambiguous price is `None`, never a guess; `parse_price(text, currency="EUR")` passes the currency you already know about the page, so that `"1.299"` reads as thousands. Do not implement your own.
- **Keep imports lazy** — heavy dependencies (Selenium, headless browsers) should be imported inside `scrape()` after a feature check, so the bot starts even if the dep is missing.
- **Cache nothing in the instance** — scrapers are singletons, so an unbounded dict on `self` is a memory leak.
- **Do not call the registry from inside `scrape()`** — keeps plugins composable and lockfree.

## Related docs

- [scrapers.md](scrapers.md) — built-in scraper inventory and the generic 9-strategy fallback chain.
- [architecture.md](architecture.md) — where scrapers fit in the data flow.
- [CONTRIBUTING.md](../CONTRIBUTING.md) — coding standards, lint/type rules, PR workflow.
