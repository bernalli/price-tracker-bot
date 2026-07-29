<p align="center">
  <img src="docs/img/cover.png" alt="price-tracker-bot — self-hosted Telegram bot for multi-site price tracking" width="100%">
</p>

# price-tracker-bot

[![CI](https://github.com/SVM-98/price-tracker-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/SVM-98/price-tracker-bot/actions/workflows/ci.yml)
[![Security](https://github.com/SVM-98/price-tracker-bot/actions/workflows/security.yml/badge.svg)](https://github.com/SVM-98/price-tracker-bot/actions/workflows/security.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Self-hosted Telegram bot for multi-site price tracking with auto-quarantine, structured observability, fine-grained notification preferences, and a plugin architecture for adding new sites.

<p align="center">
  <img src="docs/img/price-chart.png" alt="Price-history chart with target line, as sent by the /chart command" width="790">
  <br>
  <em>The <code>/chart</code> command: price history with your target line, rendered by the bot.</em>
</p>

## Why this bot

| Feature                          | price-tracker-bot | Camelcamelcamel | Keepa | Pricepulse |
| -------------------------------- | ----------------- | --------------- | ----- | ---------- |
| Self-host                        | ✅                 | ❌               | ❌     | ❌          |
| Multi-site (17 built-in)         | ✅                 | ❌ (Amazon only) | ❌     | partial    |
| Plugin extension point           | ✅                 | ❌               | ❌     | ❌          |
| Full observability (Prom+Grafana)| ✅                 | ❌               | ❌     | ❌          |
| Fine-grained notifications       | ✅                 | basic           | basic | basic      |
| Open-source (MIT)                | ✅                 | ❌               | ❌     | ❌          |

## Key features

- 17 built-in scrapers (Amazon, eBay, Shopify-generic, Walmart, Target, BestBuy, Etsy, Newegg, Wayfair, MediaMarkt, Otto, Zalando, Apple Store, Google Store, AliExpress, Generic JSON-LD/microdata/OG/RDFa chain, Playwright fallback)
- Per-domain auto-quarantine with tier-based exponential backoff (closes infinite-429 loops)
- Multi-currency price tracking (Decimal precision, ECB rates with persistent TTL cache)
- Outlier detection via median ratio (rejects bogus parses without polluting price history)
- Notification preferences: mute, digest, quiet hours, throttle, timezone-aware, per-product
- Prometheus exporter on `127.0.0.1:9090` + structured JSON logging via structlog
- Grafana dashboard with 14 panels (latency, block rate, quarantine map, alerts, currency)
- Plugin extension point at `plugins/` for custom scrapers
- Bilingual UI (English + Italian) with auto-detect from Telegram `language_code`
- Hardened Docker deploy: non-root, read-only root fs, dropped capabilities, no-new-privileges, resource limits

## Quick start

```bash
git clone https://github.com/SVM-98/price-tracker-bot.git
cd price-tracker-bot
cp .env.example .env
# edit .env: set TELEGRAM_BOT_TOKEN and ALLOWED_USERS
docker compose up -d
docker compose logs -f price-tracker-bot
```

Send `/start` to your bot from Telegram. The first user listed in `ALLOWED_USERS` is auto-promoted to admin.

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and fill in:

| Variable                | Default                  | Description                                                                      |
| ----------------------- | ------------------------ | -------------------------------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`    | (required)               | Telegram bot API token                                                           |
| `ALLOWED_USERS`         | (required)               | Comma-separated Telegram user IDs authorized to use the bot (first listed becomes admin) |
| `DATABASE_PATH`         | `/data/pricetracker.db`  | SQLite database path                                                             |
| `LANG`                  | `en`                     | Default locale fallback when Telegram language_code missing                      |
| `PROMETHEUS_BIND`       | `127.0.0.1:9090`         | Prometheus exporter bind address (host:port)                                     |
| `LOG_LEVEL`             | `INFO`                   | structlog log level                                                              |

See [docs/operations.md](docs/operations.md) for full operational reference.

## Commands

### Monitoring
- `/start` — register and view main menu
- `/list` — list tracked products with current prices
- `/add <url>` — start tracking a product
- `/remove <product_id>` — stop tracking
- `/details <product_id>` — full info + price history
- `/chart <product_id> [days]` — matplotlib price chart
- `/pause <product_id>` / `/reactivate <product_id>` — temporarily stop checks
- `/set_interval <product_id> <minutes>` — custom check interval (5 min – 7 days)

### Settings
- `/threshold <product_id> <pct|fixed|target_price>` — alert threshold
- `/notification_mode <immediate|digest>` — global notification mode
- `/health` — view scraper health + quarantine status

### Notification preferences (per-user)
- `/mute <product_id|all> [duration]` — silence alerts
- `/unmute <product_id|all>` — restore alerts
- `/digest_mode <on|off>` — batch alerts into periodic digest
- `/digest_now` — flush pending digest immediately
- `/quiet_hours <HH:MM-HH:MM>` — silent window (timezone-aware)
- `/timezone <IANA>` — your timezone (e.g. `Europe/Rome`)
- `/throttle <max_per_hour>` — sliding-window rate limit
- `/prefs` — view current preferences

### Admin
- `/adduser <telegram_id>` — authorize a user
- `/removeuser <telegram_id>` — revoke authorization
- `/users` — list authorized users
- `/nick <telegram_id> <nickname>` — assign display nickname

## Supported sites

See [docs/scrapers.md](docs/scrapers.md) for the full list of 17 built-in scrapers with status, coverage, and notes. Generic fallback (`GenericScraper`) handles any site exposing JSON-LD, microdata, OpenGraph, or RDFa product metadata.

## Observability

Prometheus metrics exposed on `127.0.0.1:9090/metrics` (counter, gauge, histogram for scraper duration, block events, quarantine state, alerts, notifications, currency lookups). Structured JSON logs via structlog. Grafana dashboard at `docs/grafana/price-tracker-dashboard.json` (14 panels). See [docs/observability.md](docs/observability.md).

## Plugin extension

Drop a custom scraper file in `plugins/<name>.py` (gitignored except `README.md`) or install a pip package with the `price_tracker.scrapers` entry-point group. See [docs/plugins.md](docs/plugins.md) for the contract and a minimal example.

## Localization

Two locales shipped: `en` (source language) and `it` (Italian translation). Runtime selection auto-detects from Telegram `language_code`, falls back to the `LANG` environment variable, then to `en`. To add a translation, see [docs/i18n.md](docs/i18n.md).

## Project structure

```
src/price_tracker/
├── bot/            # Telegram interface (handlers, decorators, messages)
├── core/           # scheduler, alert engine, outlier detection, health, currency
├── scrapers/       # 17 built-in site-specific scrapers + generic chain
├── db/             # SQLite repository, models, versioned migrations
├── notifier/       # delivery, preferences, digest, throttle
├── observability/  # metrics, structured logging
└── locale/         # gettext catalogs (en, it_IT)
plugins/            # extension point for custom scrapers
docs/               # user + contributor documentation
tests/              # pytest suite (≥430 tests, ≥90% coverage)
```

## Roadmap

- v0.1.0 — first public release (Plan 4 milestone): GitHub push + ghcr.io image
- post-v0.1.0 — community plugins, additional locales, dashboard variants

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). All contributions welcome: bug reports, feature suggestions, scraper plugins, translations, dashboard panels.

## License

MIT — see [LICENSE](LICENSE).
