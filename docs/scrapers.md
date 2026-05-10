# Scrapers

`price-tracker-bot` ships **17 built-in scrapers** plus two fallbacks (a generic structured-data extractor and a Playwright-based renderer for JavaScript-heavy sites). All are registered at startup and resolved by URL host via the central registry. Custom scrapers can be added as drop-in plugins or pip packages — see [plugins.md](plugins.md).

## Built-in inventory

| Domain(s)                                                              | Class                       | Priority | Notes                                                |
| ---------------------------------------------------------------------- | --------------------------- | -------- | ---------------------------------------------------- |
| `amazon.{com,it,de,co.uk,fr,es,nl,pl,se,ca,com.au,co.jp}`, `amzn.{eu,to}` | `AmazonScraper`            | 100      | High priority; CAPTCHA detection                      |
| `ebay.{com,it,de,co.uk,fr,es,nl,pl,com.au,ca}`                          | `EbayScraper`              | 90       | Auction & buy-it-now formats                          |
| `*.myshopify.com` and Shopify-powered storefronts                        | `ShopifyScraper`           | 80       | Generic Shopify `/products/<slug>.json` endpoint      |
| `aliexpress.{com,it,fr,de,es,ru,nl}`                                    | `AliexpressScraper`        | 50       | Parses inline `window.runParams` JSON                 |
| `apple.com` (incl. subdomains)                                          | `AppleStoreScraper`        | 50       | Locale → currency mapping                             |
| `bestbuy.{com,ca}`                                                       | `BestbuyScraper`           | 50       | JSON-LD primary path                                  |
| `etsy.com`                                                               | `EtsyScraper`              | 50       | Decimal price + currency                              |
| `store.google.com`                                                       | `GoogleStoreScraper`       | 50       | Pixel/Pixel Watch lineup                              |
| `mediamarkt.{de,it,...}`                                                 | `MediamarktScraper`        | 50       | EU MediaMarkt locales                                 |
| `newegg.com`                                                             | `NeweggScraper`            | 50       | Tech/components; PDP API fallback                     |
| `otto.de`                                                                | `OttoScraper`              | 50       | DE marketplace; cookie banner handling                |
| `target.com`                                                             | `TargetScraper`            | 50       | TCIN extraction + JSON-LD                             |
| `walmart.com`                                                            | `WalmartScraper`           | 50       | NextData JSON parsing                                 |
| `wayfair.{com,co.uk,ca,de}`                                              | `WayfairScraper`           | 50       | Furniture/home goods                                  |
| `zalando.{it,de,fr,es,nl,co.uk,pl,...}`                                  | `ZalandoScraper`           | 50       | EU fashion locales                                    |
| (any URL not matched by a site-specific scraper)                         | `PlaywrightFallbackScraper`| 10       | Headless Chromium render; requires `playwright` package, returns error if unavailable |
| (rare; only if PlaywrightFallback errors before resolution)              | `GenericScraper`           | 0        | Last-resort structured-data extractor; runs the 9-strategy chain |

> Total: **17** (15 site-specific + `GenericScraper` + `PlaywrightFallbackScraper`).

All scrapers return prices as `Decimal` (never `float`). Outlier detection via median ratio rejects bogus parses without polluting price history.

## Resolution algorithm

The registry sorts scrapers by `priority` (descending). On each URL lookup (`registry.resolve(url)`), the registry walks the sorted list and returns the first scraper whose `can_handle(url)` returns `True`. Site-specific scrapers implement `can_handle` as a regex match against `domain_patterns`. Both fallbacks (`PlaywrightFallbackScraper`, `GenericScraper`) override `can_handle` to broaden their reach: `PlaywrightFallbackScraper` returns `True` for every URL (and earns precedence via its higher priority), so `GenericScraper` is reached only when Playwright is selectively unregistered or its `can_handle` is bypassed by the caller.

```
resolution order:
  AmazonScraper (100)
  EbayScraper (90)
  ShopifyScraper (80)
  AliexpressScraper, AppleStoreScraper, BestbuyScraper,
  EtsyScraper, GoogleStoreScraper, MediamarktScraper,
  NeweggScraper, OttoScraper, TargetScraper, WalmartScraper,
  WayfairScraper, ZalandoScraper (50, alphabetical tie-break)
  PlaywrightFallbackScraper (10, always-handles)
  GenericScraper (0, last resort)
```

The first match wins; lower-priority scrapers are not tried even on parse failure (the failure is recorded in `scraper_health` and the domain is quarantined per the HealthManager policy).

## Generic scraper extraction chain

`GenericScraper` runs a 9-strategy extraction chain when no site-specific scraper handles a URL. Each strategy attempts to fill `price`, `name`, and `currency` from a different source. The loop **accumulates** results across strategies (a strategy contributing `price` does not block a later strategy from contributing `name`) and **breaks** as soon as both `price` and `name` are resolved. If all 9 strategies leave either field empty, the parser returns a `ProductInfo` with the partial data and a low-confidence flag.

Strategy order (executed top-to-bottom):

1. **JSON-LD** — `<script type="application/ld+json">` with `Product` / `Offer` schema
2. **Microdata** — `itemtype="http://schema.org/Product"` and friends
3. **OpenGraph** — `<meta property="product:price:amount">`, `og:title`
4. **RDFa** — `property="schema:price"`, `property="og:price:amount"`
5. **Meta tags** — non-OG/non-RDFa price/title `<meta>` hints
6. **CSS selectors** — common e-commerce class names (`.price`, `.product-title`, etc.)
7. **Data attributes** — `data-price`, `data-product-*` HTML attributes
8. **JS product data** — inline `<script>` blobs (`window.__INITIAL_STATE__`, dataLayer pushes, etc.)
9. **Regex** — heuristic regex on visible text (warn-only; logged at low confidence)

## Test fixtures and tests pattern

Each built-in scraper has at least one HTML fixture and a parametrized test:

- **Fixtures**: `tests/fixtures/<name>/<descriptive>.html` (one or more pages per scraper)
- **Tests**: `tests/unit/scrapers/test_<name>.py` — parametrized over fixtures, asserting `(title, price, currency, is_available)` against expected values.
- **Coverage gate**: per-scraper ≥80% line coverage (see [CONTRIBUTING.md](../CONTRIBUTING.md)).

Adding a fixture for a new product layout is the recommended way to lock down a parser bug or a new product variant. See [plugins.md](plugins.md) for the same convention applied to plugin scrapers.

## Adding a built-in scraper vs a plugin

| Aspect          | Built-in (this repo)                          | Plugin (drop-in)                                    |
| --------------- | --------------------------------------------- | --------------------------------------------------- |
| Source location | `src/price_tracker/scrapers/<name>.py`        | `plugins/<name>.py` (gitignored) or pip package      |
| Discovery       | `core.registry.discover_builtin_scrapers`     | `core.registry.discover_dropin_scrapers` + entry-points |
| Tests           | `tests/unit/scrapers/test_<name>.py` (required) | recommended (suite of choice)                      |
| Maintenance     | Project maintainer (PR review, CI gates)      | Plugin author                                        |
| Distribution    | Bundled in every release                       | Independent; managed by user                          |

For built-in additions, follow the existing scraper pattern (subclass `AbstractScraper`, set `domain_patterns` and `priority`, implement `async def scrape`). For plugins, see [plugins.md](plugins.md).
