# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

(empty)

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
  backoff: closes bug #1 (xteink.com infinite 429 loop).
- NotificationPrefs system with 8 commands: `/mute`, `/unmute`, `/digest_mode`,
  `/quiet_hours`, `/timezone`, `/throttle`, `/prefs`, `/digest_now`.
- DigestService for batched alerts with periodic flush.
- Prometheus exporter on `127.0.0.1:9090` with counter/gauge/histogram
  metrics for scraper duration, block events, quarantine state, alerts
  sent/skipped, currency lookups.
- Structured JSON logging via structlog.
- Grafana dashboard with 14 panels.
- Plugin extension point at `plugins/` for custom scrapers (entry-point
  group `price_tracker.scrapers` + auto-discovery).
- Bilingual UI (English source + Italian translation) with auto-detect
  from Telegram `language_code`, fallback to `LANG` environment variable.
- Generic scraper extraction chain: JSON-LD, microdata, OpenGraph,
  RDFa, heuristic regex.
- Versioned database migrations (001-010) replacing inline ALTER TABLE
  statements.
- Tenacity-based retry policy replacing ad-hoc `2**attempt` loops.
- Persistent ECB currency rate cache with TTL.
- Comprehensive test suite with at least 430 tests, at least 90% global
  coverage, at least 93% core coverage, at least 80% per-scraper coverage.
- GitHub Actions CI/CD: ci.yml (matrix py3.11/3.12/3.13), security.yml,
  release.yml (tag-triggered GitHub Release with sdist + wheel),
  docker-build.yml (multi-arch verify-only).
- Dependabot for pip + github-actions weekly updates.
- Issue templates and PR template.
- Documentation site: README, architecture, observability, scrapers,
  plugins, notifications, operations, i18n.
- Contributor docs: CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md.

### Changed
- `bot.py` monolith (2664 LOC) split into modular
  `bot/handlers/{auth,monitoring,settings,product,history,debug,...}.py`.
- `database.py` monolith (807 LOC) split into
  `db/{models,repository,migrator}.py`.
- `checker.py` (609 LOC) split into `core/{scheduler,alert,outlier}.py`.
- All exception handlers narrowed from broad `except Exception` to
  specific exception types (BLE001 enforced via ruff).
- Container deploy hardened: read-only root filesystem, capability drop,
  no-new-privileges, resource limits.

### Fixed
- Bug #1: infinite 429 loop on xteink.com (HealthManager auto-quarantine).
- Bug #2: 27+ broad `except Exception` (ruff BLE001 enforced).
- Bug #3: zero test coverage (now at least 430 tests).
- Bug #4: bot.py 2664 LOC monolith (split into modules).
- Bug #5: 22 inline ALTER TABLE without migration versioning
  (versioned migrations).
- Bug #6: ad-hoc `2**attempt` retry (tenacity).
- Bug #7: checker.py mixing concerns (split into modules).
- Bug #8: ECB currency cache lost on restart (persistent
  cache with TTL).
- Bug #9: container deploy without read-only root, resource limits, or
  `.dockerignore` (hardening).

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
