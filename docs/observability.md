# Observability

`price-tracker-bot` exposes a Prometheus exporter on `127.0.0.1:9090` and structured JSON logs on stdout. A pre-built Grafana dashboard with 14 panels is shipped at `docs/grafana/price-tracker-dashboard.json`.

## Metrics endpoint

`GET http://127.0.0.1:9090/metrics`

Bound to localhost only by default (`PROMETHEUS_BIND=127.0.0.1:9090`). Expose externally via a reverse proxy (nginx, Caddy, Traefik) if your Prometheus server runs on a different host. Enabled by default; set `METRICS_ENABLED=0` to disable.

Smoke check:

```bash
curl -fsS http://127.0.0.1:9090/metrics | head -30
```

You should see `# HELP price_tracker_*` lines for every metric below.

## Metrics catalog

All metric names are prefixed with `price_tracker_` (NAMESPACE constant at `observability/metrics.py:20`).

| Metric                                       | Type      | Labels                                | Description                                                       |
| -------------------------------------------- | --------- | ------------------------------------- | ----------------------------------------------------------------- |
| `price_tracker_price_check_total`            | Counter   | `scraper`, `domain`, `status`         | Price-check attempts; `status` ∈ `success`/`outlier`/`error`/`no_alert` |
| `price_tracker_scraper_duration_seconds`     | Histogram | `domain`, `scraper`                   | End-to-end scrape duration (HTTP + parse)                         |
| `price_tracker_outlier_rejected_total`       | Counter   | `scraper`, `domain`                   | Outlier-detection rejections (median ratio)                       |
| `price_tracker_notification_sent_total`      | Counter   | `type`, `channel`                     | Notifications delivered; `type` ∈ `immediate`/`digest`, `channel=telegram` |
| `price_tracker_notification_skipped_total`   | Counter   | `reason`                              | Notifications skipped; `reason` ∈ `mute`/`quiet_hours`/`throttle`/`digest_pending` |
| `price_tracker_quarantine_state`             | Gauge     | `domain`, `state`                     | Per-domain quarantine state: 0=CLOSED, 1/2/3=LOCKED (tiers T1/T2/T3), 4=HALF_OPEN |
| `price_tracker_quarantine_transitions_total` | Counter   | `domain`, `from_state`, `to_state`    | State machine transitions in HealthManager                         |
| `price_tracker_quarantine_skip_total`        | Counter   | `domain`                              | Scrape attempts skipped because domain is locked                   |
| `price_tracker_currency_lookups_total`       | Counter   | `result`                              | FX-rate lookups; `result` ∈ `hit`/`miss`/`error`/`fallback` (ECB source) |
| `price_tracker_currency_cache_hit_rate`      | Gauge     | (none)                                | Rolling cache hit ratio (0.0–1.0)                                  |
| `price_tracker_products_tracked_total`       | Gauge     | (none)                                | Active product rows in the database                                |
| `price_tracker_bot_uptime_seconds`           | Gauge     | (none)                                | Bot uptime since process start                                     |
| `price_tracker_scheduler_jobs_active`        | Gauge     | (none)                                | Currently running scrape jobs                                      |
| `price_tracker_digest_queue_size`            | Gauge     | (none)                                | Pending alerts in the digest queue                                 |

## Grafana dashboard

Import `docs/grafana/price-tracker-dashboard.json` into Grafana. The dashboard targets the Prometheus data source by default; adjust the `datasource` UID at import time if needed.

The 14 panels (in default order):

1. **Uptime** — `price_tracker_bot_uptime_seconds`
2. **Products tracked** — `price_tracker_products_tracked_total`
3. **Scheduler jobs active** — `price_tracker_scheduler_jobs_active`
4. **Scrape rate by status** — `rate(price_tracker_price_check_total[5m])` grouped by `status`
5. **Scraper p95 latency** — `histogram_quantile(0.95, sum by (le, domain) (rate(price_tracker_scraper_duration_seconds_bucket[5m])))`
6. **Locked domains** — count of `price_tracker_quarantine_state` in [1, 3] (locked tiers; T1/T2/T3)
7. **Quarantine state matrix** — heatmap of `price_tracker_quarantine_state` over `domain` × time
8. **Quarantine transitions** — `rate(price_tracker_quarantine_transitions_total[15m])` by `from_state`, `to_state`
9. **Notifications sent** — `rate(price_tracker_notification_sent_total[5m])` by `type`
10. **Notifications skipped** — `rate(price_tracker_notification_skipped_total[5m])` by `reason`
11. **Digest queue size** — `price_tracker_digest_queue_size`
12. **Outlier rejections** — `rate(price_tracker_outlier_rejected_total[15m])` by `domain`
13. **Currency cache hit rate** — `price_tracker_currency_cache_hit_rate`
14. **Errors by domain** — `rate(price_tracker_price_check_total{status="error"}[5m])` by `domain`

## Structured logs

Logging uses [structlog](https://www.structlog.org/) with a JSON renderer to stdout. Configuration is in `src/price_tracker/observability/logging.py`. Some keys are bound via `structlog.contextvars` for async-stack propagation; others are passed as keyword arguments to individual log calls. The boundary depends on the call site.

Set the level via `LOG_LEVEL` (default `INFO`). Other valid values: `DEBUG`, `WARNING`, `ERROR`.

Common bound keys you'll see in logs:

| Key            | Meaning                                                |
| -------------- | ------------------------------------------------------ |
| `event`        | Short identifier for the log event (string)            |
| `domain`       | URL netloc (e.g. `amazon.com`)                         |
| `scraper`      | Scraper class name (e.g. `AmazonScraper`)              |
| `product_id`   | DB row id of the tracked product                       |
| `user_id`      | Telegram user id                                       |
| `duration_ms`  | Wall-clock duration of the operation                   |
| `error`        | Exception class + message (when applicable)            |
| `level`        | Log level (auto-added by `structlog.processors.add_log_level`) |
| `timestamp`    | ISO 8601 with timezone (auto-added)                    |

Example log line (pretty-printed; actual output is single-line JSON):

```json
{
  "event": "scrape_complete",
  "domain": "amazon.it",
  "scraper": "AmazonScraper",
  "product_id": 42,
  "duration_ms": 312,
  "status": "success",
  "level": "info",
  "timestamp": "2026-05-15T08:42:11.123456+00:00"
}
```

## Suggested Prometheus alerting rules

These are starting points; tune thresholds and `for` durations to your traffic profile. Deploy via Prometheus's `rule_files` config or Alertmanager.

```yaml
groups:
  - name: price-tracker
    rules:
      - alert: ScraperBlockRateHigh
        expr: rate(price_tracker_quarantine_transitions_total{to_state=~"LOCKED_.*"}[5m]) > 0.05
        for: 10m
        annotations:
          summary: "Scraper block rate elevated on {{ $labels.domain }}"

      - alert: MultipleQuarantines
        expr: count(price_tracker_quarantine_state >= 1 and price_tracker_quarantine_state <= 3) > 3
        for: 5m
        annotations:
          summary: "More than 3 domains quarantined simultaneously"

      - alert: SchedulerStuck
        expr: rate(price_tracker_price_check_total[30m]) == 0
        for: 30m
        annotations:
          summary: "Scheduler has not produced a price-check event in 30 minutes"

      - alert: NotificationFailureSurge
        expr: rate(price_tracker_notification_skipped_total{reason!~"mute|quiet_hours"}[10m]) > 0.1
        for: 10m
        annotations:
          summary: "Non-user-driven notification skips elevated"
```

## Related docs

- [architecture.md](architecture.md) — where metrics fit in the data flow.
- [operations.md](operations.md#monitoring-quick-start) — endpoint binding and curl smoke check.
- [notifications.md](notifications.md) — alert flow and skip reasons.
