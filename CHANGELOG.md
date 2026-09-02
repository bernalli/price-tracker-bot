# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

(empty)

## [0.2.0] - 2026-07-27

Alert-correctness release. A tracked Amazon product reported three "price drop"
alerts for a price that never existed; investigating it surfaced a family of
related defects where a single unverified reading, or a setting nobody read,
decided what the user was told.

### Fixed
- **A single bad scrape can no longer raise an alert.** A reading that
  contradicts recent history (a drop below 60% of the median, or a rise above
  2.5x it) is now held and must be confirmed by a run of consecutive agreeing
  checks before it is persisted or alerted on. Field case: a product steady at
  ~386 EUR reported 187.95 EUR on isolated checks, each bouncing back to ~386 on
  the next one, and each firing a "-51.3%" alert. Genuinely deep discounts still
  alert — a couple of check intervals later — because a real price repeats and a
  glitch does not. The previous guard could not catch this: it only rejected
  readings below 1/50th of the median, i.e. under ~7.70 EUR on that product.

  The confirmation count was calibrated by replaying the product's real 2424-reading
  history rather than guessed: it removes every false alert in the incident
  window (10 of them) while preserving both genuine alerts. Tunable via
  `READ_CONFIRMATIONS` — it costs `(N-1) × check_interval` of latency on a real
  steep discount, so long check intervals may want a lower value.
- **Products no longer get stuck rejecting a real price change forever.** A
  reading rejected as an outlier never entered price history, so the median that
  rejected it could never move; one product had been rejecting the same
  legitimate 9.99 → 34.99 repricing on every check for days. Sustained new
  prices now confirm themselves and are accepted.
- **Used and refurbished offers are no longer tracked as the new-product
  price.** Scrapers already reported buy-box condition and the bot already let
  you pin one, but the scheduler read neither. Amazon condition detection also
  treated "there is a price in the buy-box" as proof the item was new, which
  made its used/Warehouse branch unreachable.
- **Alerts that Telegram refused are no longer recorded as sent.** A failed
  delivery started the 24-hour cooldown, suppressing later drops in favour of a
  message the user never received.
- `/target` now actually alerts. The target price was stored and never
  evaluated: only the percentage/absolute threshold was, so a target reached by
  a series of small moves passed in silence.

### Added
- Availability tracking: a sold-out listing is recorded as unavailable, and a
  **back-in-stock** notification is sent when it returns. The scrapers had been
  reporting availability all along and the scheduler discarded it.
- Scheduled price alerts now honour notification preferences (mute, quiet hours,
  throttle, digest). The preference engine existed and was fully tested, but had
  no production caller on the periodic path — every setting was inert for
  exactly the alerts it was meant to govern.

### Note on enabling quiet hours
Now that preferences are actually applied, an alert landing inside a quiet
window is not delivered at that moment — it is queued for the next digest if
digest mode is on, and otherwise dropped. A dropped alert does **not** start the
anti-flap cooldown, so if the price is still down at the next check once the
window has passed, you are told then.

### Known issues
Found during the same audit, not addressed here:
- Percentage/absolute thresholds compare against the last reading, so a gradual
  slide (5% + 5% + 5%) never crosses a 10% threshold.
- The alert cooldown does not model separate drop episodes: a recovery followed
  by a fresh drop can be suppressed within the cooldown window.
- Per-product check intervals are stored but the scheduler checks everything on
  the global tick.
- Price-history retention is implemented but never scheduled, so the database
  grows without bound.
- Shopify tracks the first available variant rather than the one in the URL.
- The price update, the history row and the held-read bookkeeping are three
  separate commits rather than one transaction. A crash between them can leave
  the new price durable while the alert decision is skipped, losing that
  crossing. Pre-existing; the confirmation gate inherits it rather than adding
  it, and closing it properly needs a transactional repository method.

## [0.1.12] - 2026-05-22

### Fixed
- Wire HealthManager pipeline into `Scheduler` (`_check_product_core` /
  `_scrape_one` / `check_user_products_for_user`). Previously the scheduler
  only *consumed* HealthManager state via `is_locked`/`is_half_open` but
  never *produced* it via `record_success`/`record_block`, so the
  `scraper_health` table stayed empty forever and auto-quarantine
  (Feature B / Bug #1 xteink 429 loop) never engaged in production.
  Each successful scrape now calls `handle_success_in_pipeline`; each
  `BlockEvent` calls `handle_block_in_pipeline`. Both skip the call when
  the eTLD+1 cannot be resolved (`domain == "unknown"`). New integration
  tests under `tests/integration/test_health_pipeline_integration.py`
  guard the contract against future regressions.

## [0.1.11] - 2026-05-17

### Fixed
- Add `brotli>=1.1.0` as a runtime dependency. `get_headers()`
  advertises `Accept-Encoding: gzip, deflate, br` to mimic a real
  browser, but httpx will only decompress brotli responses when
  `brotli` (or `brotlicffi`) is importable. Without it, sites that
  serve brotli by default (e.g. `nove25.net`) returned a 55 KB
  skeleton instead of the 436 KB full document, silently stripping the
  JSON-LD `Product` block, the `og:price:amount` meta tag and the
  `itemprop="price"` microdata. Result: the new Nove25 scraper shipped
  in v0.1.10 returned `price=None` against the real site even though
  every offline fixture-based test passed. Regression test
  `test_brotli_available_for_httpx_decompression` fails CI if the
  dependency disappears again.

## [0.1.10] - 2026-05-17

### Added
- New built-in scraper `nove25` (priority 75) for `nove25.net`.
  Reads price/currency from static HTML via a 4-step fallback chain:
  JSON-LD `Product.offers.price` → OpenGraph `product:price:amount` →
  microdata `itemprop=price` → CSS `.product-price`. No JavaScript
  required (the previous `playwright_fallback` path was failing on this
  site even though the price is fully present in the initial document).

### Fixed
- Shopify scraper: validate the **final URL** after redirects and reject
  any response whose path is not `/products/<slug>`. Closes two classes
  of historical bug:
  - dead product URLs that 301-redirected to the store home let the HTML
    fallback parse a random price from the home and silently save the
    home's `og:title` as the product name (e.g. `Filling Pieces®
    Official Webshop`);
  - collection URLs (`/collections/<slug>?page=N`) on a
    `KNOWN_SHOPIFY_DOMAIN` passed `can_handle` and then leaked a price
    from one of the listed products plus the collection's `og:title`
    (e.g. `Men`) as a "product".

### Internal
- `_fetch_shopify_html` renamed to `_fetch_shopify_response` to expose
  `response.url` for the post-redirect validation. Call sites updated.

## [0.1.8] - 2026-05-17

### Fixed
- Every ``/history`` and per-product "📊 Storico prezzo" chart silently
  returned ``"📭 Dati insufficienti per generare il grafico (servono
  almeno 2 punti)"`` even on products with hundreds of history rows.
  ``_generate_chart`` accesses each row as ``record["checked_at"]`` /
  ``record["price"]`` (dict-style); ``PriceHistoryRecord`` was the only
  ``@dataclass`` record in ``db/models.py`` that did **not** extend
  ``_DictCompatMixin`` (added in v0.1.4 to ``UserRecord`` and
  ``ProductRecord``). The subscript raised ``TypeError`` and the chart
  helper swallowed it as "no data", making the bug invisible in logs.

### Added
- Contract test ``test_price_history_record_supports_dict_access``
  exercises the three mixin paths (``record[key]``, ``record.get(key)``,
  ``key in record``) on a real ``PriceHistoryRecord`` returned by the
  repository. A future refactor cannot drop the mixin without failing CI.

## [0.1.7] - 2026-05-17

### Fixed
- `/checkall` and the menu **🔍 Check all** button took ~5 minutes for 31
  products because the v0.1.6 pull-mode methods inherited the same
  `delay_between_products = 5s` gentle pacing as the periodic background
  job. The polite 5s is correct when the bot is the one scheduling the
  scrape; it is the wrong UX trade-off when the user is waiting live for
  the answer.

### Changed
- `Scheduler.check_user_products_for_user` accepts an optional
  ``delay_between_products`` kwarg. The periodic job leaves it unset and
  inherits the default 5s (unchanged). Interactive handlers (``/checkall``,
  menu **🔍 Check all** button) override it to ``0.5s`` — 31 products now
  finish in ~1–2 minutes instead of ~5.

## [0.1.6] - 2026-05-16

### Fixed
- `/check`, `/checkall`, the menu **🔍 Check all** button and the per-product
  inline **Check now** button all crashed with `ModuleNotFoundError: No
  module named 'checker'` (and `_send_alert` would also have crashed on
  `chart` for the photo path). Plan 1 F1 left **seven** deferred imports of
  the legacy bare module names (`from checker import PriceChecker, format_alert`
  in `bot/handlers/monitoring.py` ×4, `bot/handlers/callbacks/_menu.py`,
  `bot/handlers/callbacks/_product.py`; `from chart import render_price_history`
  in `_send_alert`). The handler call sites also depended on a
  `PriceChecker.check_product / check_products / check_all` API that no
  longer exists in the post-refactor codebase.
- Removed the orphan `scheduled_check` function in
  `bot/handlers/monitoring.py` — it was a duplicate of `main.scheduled_check_job`
  with broken imports and was never registered with the job queue.

### Changed
- `core.scheduler.Scheduler` gained a pull-mode API used by the interactive
  Telegram handlers:
  - `_check_product_core(product_id, ...)` — extracted from `_check_product`,
    returns `(user_id, PriceAlert) | None` instead of pushing via notifier.
  - `check_one_product_for_user(*, product_id, user_id)` →
    `CheckResult` — used by `/check` and the inline **Check now** button.
  - `check_user_products_for_user(*, user_id)` → `list[CheckResult]` — used
    by `/checkall` and the menu **🔍 Check all** button. Respects the same
    quarantine / half-open / per-tick pacing rules as the periodic job.
  - New `CheckResult` dataclass (`product_id`, `user_id`,
    `alert: PriceAlert | None`).
- The push-mode periodic job (`Scheduler.run_check_all` /
  `Scheduler.run_check_for_user`) is unchanged: the notifier callback still
  receives `(user_id, formatted_text)` as before.
- `bot/handlers/monitoring._send_alert` migrated to the new modules:
  `format_alert` is imported from `price_tracker.core.alert` and the chart
  is rendered through `bot/handlers/history._generate_chart`. The function
  now accepts an explicit `chat_id` (interactive handlers pass it) and
  falls back to `alert.owner_user_id` for legacy callers.

### Added
- `test_no_stale_imports.py` now scans for **any** legacy top-level module
  name (`scrapers`, `checker`, `chart`, `database`, `config`, `notifier`,
  `currency`, `health`, `utils`), not just `scrapers`. Catches the v0.1.6
  class of bug for every legacy name in one regex.
- `tests/integration/test_handler_import_smoke.py` literally imports every
  module under `bot/handlers/**` and every deferred `from X import Y` inside
  handler function bodies. A `ModuleNotFoundError` now fails CI instead of
  crashing the bot for a real user.
- Four scheduler tests covering the new pull-mode methods
  (alert-on-drop, no-drop-no-alert, multi-product accumulation, locked
  domain skip).

## [0.1.5] - 2026-05-14

### Fixed
- `/add <url>` still crashed after v0.1.4 with
  `TypeError: Repository.add_product() got an unexpected keyword
  argument 'price'` (and `target_price`). v0.1.4 fixed the missing
  Repository methods but did not catch **keyword-argument signature
  drift** on methods that *did* exist. `bot/handlers/product.py:335`
  and `bot/handlers/product_io.py:162` were still calling
  `add_product(price=..., target_price=..., threshold_value="10")`
  against the post-F1 signature
  `add_product(*, initial_price=..., threshold_value: Decimal=...)`
  (no `target_price` keyword).
- Aligned both call sites: `price` → `initial_price`,
  `threshold_value` is now a `Decimal`, and `target_price` is applied
  via `set_target_price(pid, target)` after `add_product` only when a
  target is set (CSV import path).

### Added
- Contract test
  `test_every_db_kwarg_exists_on_repository_signature` parses every
  `bot/**/*.py` with `ast`, extracts each `db.<method>(kw=...)` call
  site, and asserts every `kw` is in
  `inspect.signature(Repository.<method>).parameters` (or that the
  method accepts `**kwargs`). Catches future signature drift before
  it reaches a real user.

## [0.1.4] - 2026-05-14

### Fixed
- The `/add <url>` flow crashed in production with
  `AttributeError: 'Repository' object has no attribute
  'get_product_by_url_for_user'` whenever a user sent a Telegram link to
  the bot. A handler-side audit revealed that the Plan 1 F1 monolith
  split left **13 distinct `db.<method>(…)` calls** in
  `src/price_tracker/bot/**` referencing repository methods that no
  longer exist post-refactor: `get_product_by_url_for_user`,
  `get_product_for_user`, `is_user_admin`, `add_user`,
  `cleanup_old_history`, `deactivate_product`, `get_active_products`,
  `get_all_products`, `get_all_users`, `get_stats`,
  `reset_initial_price`, `set_product_interval`,
  `set_product_preferences` (≈70 call sites total).
- `ProductRecord` / `UserRecord` were defined as typed
  `@dataclass(frozen=True)` after the refactor but most handlers still
  treat rows as dicts (`product.get("name")`, `product["id"]`,
  `"is_active" in product`). The mismatch was masked by a
  `cast("dict[str, Any] | None", ...)` in `bot/handlers/_helpers.py` and
  would have crashed every read path as soon as it was exercised.

### Added
- 13 thin wrapper methods on `Repository` that delegate to the existing
  typed API (e.g. `get_active_products` → `list_products_for_user(only_active=True)`,
  `cleanup_old_history` → `delete_old_price_history`, etc.) plus a new
  query for `get_product_by_url_for_user` and a `get_stats` helper that
  returns `{active_products, total_products, total_checks}` scoped per
  user or globally.
- `_DictCompatMixin` on `ProductRecord` and `UserRecord` providing
  `__getitem__`, `get(key, default)` and `__contains__` so legacy
  handler code keeps working without copying every row into a dict.
- `tests/integration/test_repository_handler_contract.py` — defense in
  depth: greps every `src/price_tracker/bot/**/*.py` for
  `db.<method>(` and asserts the method exists on `Repository`. Any
  future drift between handler calls and the repository surface fails
  the test rather than the user. 13 new tests (449 total, 90.14%
  coverage).

## [0.1.3] - 2026-05-14

### Fixed
- `/add <url>` and CSV import (`/import`) crashed with
  `ModuleNotFoundError: No module named 'scrapers'` in
  `bot/handlers/product.py` and `bot/handlers/product_io.py`, plus the
  `/debug` command crashed with `AttributeError` because
  `ScraperRegistry` was being called like an individual scraper. Plan 1
  F1 monolith split left two stale deferred imports
  (`from scrapers import identify_site`) and three call sites still
  invoking `scraper.scrape(url, client)` directly on the
  ``ScraperRegistry`` instance instead of going through
  ``registry.resolve(url).scrape(url, client)`` like the scheduler does.
  None of the affected flows had test coverage post-refactor.
- Replaced `identify_site` with `core.url_utils.extract_etld_plus_one`
  (the post-refactor equivalent for "domain"), and aligned the three
  handler call sites to the scheduler pattern (resolve → guard against
  ``None`` → scrape).
- Added regression test `tests/unit/test_no_stale_imports.py` that
  greps every `src/price_tracker/**/*.py` for top-level ``scrapers``
  imports — future drift fails the test, not the user.

## [0.1.2] - 2026-05-14

### Fixed
- Bot command handlers crashed with `KeyError: 'db'` / `KeyError:
  'scraper'` and surfaced the generic
  `"❌ Si è verificato un errore. Riprova tra qualche istante."` to
  Telegram users on every command going through `bot.decorators._db` /
  `_scraper` (add/list/history/debug/monitoring/callbacks). Root cause:
  the Plan 1 F1 monolith split renamed the bootstrap keys to
  `bot_data["repo"]` / `["registry"]` but left the handler-side lookups
  expecting `["db"]` / `["scraper"]`. Added the two missing aliases in
  `main.post_init` and a regression test
  (`test_post_init_populates_all_handler_lookup_keys`) that enumerates
  every key looked up by decorators/handlers and asserts none is
  missing after `_combined_post_init` runs.

## [0.1.1] - 2026-05-14

### Fixed
- Startup wiring: `main.amain()` now invokes `_combined_post_init`
  explicitly after `Application.initialize()`. python-telegram-bot ≥22
  no longer auto-runs `post_init` from `initialize()` — it only does
  so from `run_polling`/`run_webhook`. The manual
  `initialize/start/start_polling` pattern (needed to keep the
  Prometheus exporter lifecycle outside PTB) therefore left
  `bot_data["scheduler"]`, `["repo"]`, `["health_manager"]`,
  `["digest_service"]` and other post-init artefacts unset, causing
  every scheduled `price_check` tick to raise `KeyError: 'scheduler'`
  and the core scraping loop to never run. Added integration
  regression test `tests/integration/test_startup_postinit.py`.

## [0.1.0] - 2026-05-10

First public release. Self-hosted Telegram bot for multi-site price tracking
with full observability, fine-grained notification preferences, scraper
auto-quarantine, and a plugin extension point.

### Added
- 17 built-in scrapers: amazon, ebay, shopify, generic, playwright_fallback
  (refactored from monolith) plus walmart, target, bestbuy, etsy, newegg,
  wayfair, mediamarkt, otto, zalando, apple_store, google_store, aliexpress.
- HealthManager with per-domain auto-quarantine and tier-based exponential
  backoff (Plan 2 F3.B): closes bug #1 (xteink.com infinite 429 loop).
- NotificationPrefs system with 8 commands: `/mute`, `/unmute`, `/digest_mode`,
  `/quiet_hours`, `/timezone`, `/throttle`, `/prefs`, `/digest_now` (Plan 2 F3.D).
- DigestService for batched alerts with periodic flush (Plan 2 F3.D).
- Prometheus exporter on `127.0.0.1:9090` with counter/gauge/histogram
  metrics for scraper duration, block events, quarantine state, alerts
  sent/skipped, currency lookups (Plan 2 F3.L).
- Structured JSON logging via structlog (Plan 2 F3.L).
- Grafana dashboard with 14 panels (Plan 2 F3.L).
- Plugin extension point at `plugins/` for custom scrapers (entry-point
  group `price_tracker.scrapers` + auto-discovery).
- Bilingual UI (English source + Italian translation) with auto-detect
  from Telegram `language_code`, fallback to `LANG` environment variable
  (Plan 3 F5).
- Generic scraper extraction chain: JSON-LD, microdata, OpenGraph,
  RDFa, heuristic regex (Plan 2 F3.M Task 31).
- Versioned database migrations (001-010) replacing inline ALTER TABLE
  statements (Plan 1 F1.5).
- Tenacity-based retry policy replacing ad-hoc `2**attempt` loops
  (Plan 1 F1.5).
- Persistent ECB currency rate cache with TTL (Plan 1 F1.5).
- Comprehensive test suite with at least 430 tests, at least 90% global
  coverage, at least 93% core coverage, at least 80% per-scraper coverage
  (Plan 1 F2 + Plan 2 + Plan 3).
- GitHub Actions CI/CD: ci.yml (matrix py3.11/3.12/3.13), security.yml,
  release.yml (tag-triggered GitHub Release with sdist + wheel),
  docker-build.yml (multi-arch verify-only) (Plan 3 F6).
- Dependabot for pip + github-actions weekly updates (Plan 3 F6).
- Issue templates and PR template (Plan 3 F6).
- Documentation site: README, architecture, observability, scrapers,
  plugins, notifications, operations, i18n (Plan 3 F4).
- Contributor docs: CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md
  (Plan 3 F4).

### Changed
- `bot.py` monolith (2664 LOC) split into modular
  `bot/handlers/{auth,monitoring,settings,product,history,debug,...}.py`
  (Plan 1 F1).
- `database.py` monolith (807 LOC) split into
  `db/{models,repository,migrator}.py` (Plan 1 F1).
- `checker.py` (609 LOC) split into `core/{scheduler,alert,outlier}.py`
  (Plan 1 F1).
- All exception handlers narrowed from broad `except Exception` to
  specific exception types (BLE001 enforced via ruff) (Plan 1 F1.5).
- Container deploy hardened: read-only root filesystem, capability drop,
  no-new-privileges, resource limits (Plan 3 F7).

### Fixed
- Bug #1: infinite 429 loop on xteink.com (HealthManager auto-quarantine,
  Plan 2 F3.B).
- Bug #2: 27+ broad `except Exception` (Plan 1 F1.5, ruff BLE001 enforced).
- Bug #3: zero test coverage (now at least 430 tests, Plan 1 F2 + Plan 2 + Plan 3).
- Bug #4: bot.py 2664 LOC monolith (Plan 1 F1 split).
- Bug #5: 22 inline ALTER TABLE without migration versioning
  (Plan 1 F1 versioned migrations).
- Bug #6: ad-hoc `2**attempt` retry (Plan 1 F1.5 tenacity).
- Bug #7: checker.py mixing concerns (Plan 1 F1 split).
- Bug #8: ECB currency cache lost on restart (Plan 1 F1.5 persistent
  cache with TTL).
- Bug #9: container deploy without read-only root, resource limits, or
  `.dockerignore` (Plan 3 F7 hardening).

### Security
- Container runs as non-root `botuser` (uid 1000).
- Read-only root filesystem with tmpfs for `/tmp` and `/home/botuser/.cache`.
- Linux capabilities dropped except minimal required set.
- `no-new-privileges` security option enabled.
- Memory and CPU limits enforced.
- gitleaks full-history scan in CI (security.yml).
- bandit static analysis in CI.
- osv-scanner dependency vulnerability scan in CI.
- Pre-commit hooks block secrets at commit time.

[Unreleased]: https://github.com/bernalli/price-tracker-bot/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/bernalli/price-tracker-bot/releases/tag/v0.1.0
