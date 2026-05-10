# Plugin scrapers

`price-tracker-bot` supports two ways to add scrapers without forking the repo:

1. **Drop-in directory** — place a `.py` file in `plugins/` (gitignored except for `README.md`). The registry auto-discovers it at startup.
2. **Pip-installable package** — declare a `price_tracker.scrapers` entry point in your distribution's `pyproject.toml`. Useful for sharing scrapers across projects or publishing to a private index.

Both forms must subclass `AbstractScraper` from `price_tracker.core.scraper_base`. The contract is identical to built-in scrapers (see [scrapers.md](scrapers.md)).

## Contract

Every scraper subclasses `AbstractScraper` and provides:

- `name: ClassVar[str]` — unique stable identifier (defaults to lowercased class name with `Scraper` suffix stripped).
- `priority: ClassVar[int]` — resolution priority. Higher wins. Site-specific scrapers use 50; the Shopify generic uses 80; Amazon and eBay use 100. Pick a value that fits your scraper's specificity.
- `domain_patterns: ClassVar[list[re.Pattern[str]]]` — list of compiled regexes against URL netloc (`urlparse(url).netloc`). The default `can_handle()` returns `True` if any pattern matches.
- `async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo` — fetch the URL with the injected `httpx.AsyncClient` (already configured with timeout, proxies, retry policy) and return a populated `ProductInfo`. Never raise on network or parse errors — return `ProductInfo(error="...")` instead.

`ProductInfo` is a dataclass with these fields (see `scraper_base.py:160`):
- `title: str | None`
- `price: Decimal | None` — always `Decimal`, never `float`
- `currency: str | None` — ISO 4217 (e.g. `"EUR"`, `"USD"`)
- `is_available: bool` — `False` for out-of-stock pages
- `error: str | None` — human-readable error if the parse failed

## Minimal example

```python
import re
from typing import ClassVar
import httpx

from price_tracker.core.scraper_base import AbstractScraper, ProductInfo, parse_price


class MyShopScraper(AbstractScraper):
    name: ClassVar[str] = "myshop"
    priority: ClassVar[int] = 50
    domain_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"^(www\.)?myshop\.(com|eu)$"),
    ]

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        try:
            response = await client.get(url, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return ProductInfo(error=f"HTTP error: {exc}")

        # Parse the response body. Use BeautifulSoup, lxml, or regex as fits.
        # Always wrap price string with parse_price() to get Decimal.
        # Return early with ProductInfo(error=...) on parse failure.

        title = "..."         # extract from HTML
        price_str = "..."     # raw price text
        price = parse_price(price_str)
        currency = "EUR"
        is_available = True

        if price is None:
            return ProductInfo(error="Could not parse price")

        return ProductInfo(
            title=title,
            price=price,
            currency=currency,
            is_available=is_available,
        )
```

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

In your distribution's `pyproject.toml`:

```toml
[project.entry-points."price_tracker.scrapers"]
myshop = "my_plugin.scraper:MyShopScraper"
```

Install with `pip install my-plugin` (in the same environment as `price-tracker-bot`). The registry walks `importlib.metadata.entry_points(group="price_tracker.scrapers")` at startup and registers every advertised class.

## Auto-discovery order

At startup the registry assembles the scraper pool from three sources, in this order:

1. **Built-in** scrapers from `src/price_tracker/scrapers/` (always loaded).
2. **Entry-point** plugins from installed pip packages (loaded if any).
3. **Drop-in** plugins from `plugins/` (loaded if any).

After all three sources are loaded, the registry sorts by `priority` (descending) and tie-breaks by registration order. There is no namespace conflict check — pick a stable, unique `name` to avoid surprises.

## Testing

Provide an HTML fixture and a unit test alongside your plugin:

```
tests/fixtures/myshop/sample_product.html   # captured offline
tests/unit/scrapers/test_myshop.py          # parametrized test
```

Minimal `tests/unit/scrapers/test_myshop.py`:

```python
from pathlib import Path

import pytest
from price_tracker.core.scraper_base import ProductInfo

from my_plugin.scraper import MyShopScraper  # or `plugins.myshop` for drop-in


@pytest.mark.asyncio
async def test_myshop_parses_sample(httpx_mock):
    fixture = Path("tests/fixtures/myshop/sample_product.html").read_text()
    httpx_mock.add_response(text=fixture)

    import httpx
    async with httpx.AsyncClient() as client:
        scraper = MyShopScraper()
        info = await scraper.scrape("https://myshop.com/p/1", client)

    assert info.error is None
    assert info.price is not None
    assert info.title
    assert info.currency == "EUR"
    assert info.is_available is True
```

Run with `pytest tests/unit/scrapers/test_myshop.py -v`. Coverage target: ≥80% for the plugin module (same gate as built-in scrapers — see [CONTRIBUTING.md](../CONTRIBUTING.md)).

## Best practices

- **Use the injected `client`** — it carries the bot's timeout, retry policy, and user-agent rotation. Do not create your own `httpx.AsyncClient` inside `scrape()`.
- **Return errors, do not raise** — `ProductInfo(error="...")` is logged and the domain is recorded in `scraper_health`. Raising will be caught by the scheduler but the error context is lost.
- **Use `parse_price()`** — handles thousands separators, currency symbols, comma decimals, and returns `Decimal | None`. Do not implement your own.
- **Keep imports lazy** — heavy dependencies (Selenium, headless browsers) should be imported inside `scrape()` after a feature check, so the bot starts even if the dep is missing.
- **Cache nothing in the instance** — scrapers are singletons, so an unbounded dict on `self` is a memory leak.
- **Do not call the registry from inside `scrape()`** — keeps plugins composable and lockfree.

## Related docs

- [scrapers.md](scrapers.md) — built-in scraper inventory and the generic 9-strategy fallback chain.
- [architecture.md](architecture.md) — where scrapers fit in the data flow.
- [CONTRIBUTING.md](../CONTRIBUTING.md) — coding standards, lint/type rules, PR workflow.
