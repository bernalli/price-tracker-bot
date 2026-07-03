# Architecture

> Reference: layered architecture, data flow, database schema, and plugin extension point.

## Overview

`price-tracker-bot` is a self-hosted Telegram bot for multi-site product price tracking. The codebase follows a **Core + Plugin** pattern: the public repository ships every site-specific scraper, the core scheduler/alert/health/notifier, the database layer, and the observability stack. The `plugins/` directory is a runtime extension point — drop-in custom scrapers can live there without forking the repo. The public/private boundary is intentionally light: only the `.env` (secrets) and `data/pricetracker.db` (user data) stay private; everything else is open.

## Layer diagram

```
src/price_tracker/
├── bot/             # Telegram interface — handlers, decorators, message templates
│   └── handlers/    # auth, monitoring, settings, product, history, debug, ...
├── core/            # scheduler, alert, outlier, health, currency, retry, http_client
├── scrapers/        # 17 site-specific scrapers + generic chain + playwright fallback
├── db/              # repository, models, versioned migrations (001-010)
├── notifier/        # telegram delivery, preferences, digest queue
├── observability/   # Prometheus metrics + structured JSON logging
└── locale/          # gettext catalogs (en, it_IT) — populated in F5
plugins/             # extension point for custom scrapers (gitignored except README.md)
```

Each top-level package has one responsibility and exposes a clear interface to the next layer. Cross-layer calls always flow downward (`bot/` → `core/` → `db/` / `scrapers/` / `notifier/`); `core/` is the orchestrator.

## Data flow — scheduler tick to notification

```
1. core.scheduler tick (every CHECK_INTERVAL_MINUTES, default 360)
2. db.repository.list_active_users() → [UserRecord]
   for each user:
     db.repository.list_products_for_user(user_id, only_active=True) → [ProductRecord]
3. for each product:
   a. core.health.HealthManager
      - if is_locked(domain): skip, record skipped_locked metric
      - elif is_half_open(domain): allow one probe only
      - else (open): proceed normally
   b. registry.resolve(url) → AbstractScraper | None  (registry from core.registry)
   c. await scraper.scrape(url, http_client) → ProductInfo
      - tenacity retry with exponential backoff
      - on failure: HealthManager.record_block(domain, reason); continue
      - on success: HealthManager.record_success(domain)
   d. core.outlier.is_outlier(new_price, history) → bool
      - reject if median ratio outside acceptable band
   e. db.repository.add_price_history(product_id, price)
   f. core.alert.crosses_threshold(old, new, threshold_type, threshold_value) → bool
      - threshold types: percentage / fixed / target_price
   g. if alert triggered:
      - notifier.preferences.resolve(user_id=..., product_id=...) → EffectivePrefs
        - encapsulates mute, digest_mode, quiet_hours, throttle, timezone
      - if EffectivePrefs allows immediate send: deps.notifier(user_id, formatted_text)
        (callable is wired to TelegramNotifier.send_alert)
      - else if digest mode: notifier.digest.enqueue(user_id, alert)
4. observability.metrics records counters/histograms throughout
5. observability.logging emits structured JSON events for every state change
```

## Database schema

SQLite database at `DATABASE_PATH` (default `/data/pricetracker.db`). 8 tables:

| Table                | Purpose                                                    | Migration       |
| -------------------- | ---------------------------------------------------------- | --------------- |
| `users`              | Authorized Telegram users + admin flag + nickname          | 001/003         |
| `products`           | Tracked products: URL, threshold, interval, state          | 001/002/006/007 |
| `price_history`      | Price points (Decimal as TEXT) + currency + ts             | 001             |
| `bot_config`         | Singleton key/value runtime config                         | 001             |
| `scraper_health`     | Per-domain block count + locked_until timestamp            | 008             |
| `notification_prefs` | Per-user mute, digest, quiet hours, timezone, throttle     | 009             |
| `digest_queue`       | Pending alerts batched for periodic flush                  | 010             |
| `schema_version`     | Migrator-managed table tracking applied versions           | (migrator)      |

Key indices:
- `idx_price_history_product` — fast price history lookup per product
- `idx_products_active` / `idx_products_user` — active-product filters per user
- `idx_scraper_health_locked_until` — quarantine state queries
- `idx_notif_prefs_user` — preference resolution per user
- `idx_digest_pending` — digest queue scan

Migrations are versioned `.sql` files in `src/price_tracker/db/migrations/` (001-010), applied at startup by `db.migrator.apply_migrations()`. The `schema_version` table records the highest applied version.

## Plugin extension point

Custom scrapers can be added without modifying the core repository:

1. **Drop-in directory**: place `plugins/<name>.py` (gitignored except `README.md`). Auto-discovered at startup via `core.registry`.
2. **Pip-installable plugin**: declare an entry point in your package's `pyproject.toml`:
   ```toml
   [project.entry-points."price_tracker.scrapers"]
   mysite = "my_plugin.scraper:MySiteScraper"
   ```
   Auto-discovered via `importlib.metadata.entry_points`.

Both forms must subclass `AbstractScraper` (`core/scraper_base.py:172`) and implement `async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo`. See [plugins.md](plugins.md) for the full contract and a minimal example.

## Cross-references

- [scrapers.md](scrapers.md) — built-in scraper inventory.
- [observability.md](observability.md) — metrics catalog + dashboard panels.
- [operations.md](operations.md) — deploy, env vars, backup, troubleshooting.
