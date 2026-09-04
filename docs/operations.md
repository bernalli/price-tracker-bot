# Operations

Deploy, configure, back up, upgrade, and troubleshoot the bot. For the architectural overview see [architecture.md](architecture.md). For metrics and dashboards see [observability.md](observability.md).

## Deploy options

### Docker compose (recommended)

```bash
git clone https://github.com/bernalli/price-tracker-bot.git
cd price-tracker-bot
cp .env.example .env
# edit .env: TELEGRAM_BOT_TOKEN + ALLOWED_USERS
docker compose up -d
docker compose logs -f price-tracker-bot
```

Compose handles: image build, volume mount for `/data`, restart policy, hardening directives (read-only root, capability drop, resource limits — see [Hardened deployment](#hardened-deployment)).

### venv standalone

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env
# edit .env
mkdir -p data && export DATABASE_PATH="$(pwd)/data/pricetracker.db"
python -m price_tracker.main
```

Standalone is useful for local development, debugging, or environments without Docker. No hardening is applied automatically — set OS-level limits as needed.

## Environment variables

All configuration is via environment variables, loaded from `.env` (or the host environment). See `.env.example` for a copy-pasteable template.

| Variable                      | Default                 | Description                                                                                                            |
| ----------------------------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`          | (required)              | Telegram bot API token                                                                                                 |
| `ALLOWED_USERS`               | (required)              | Comma-separated Telegram user IDs authorized to use the bot (first listed becomes admin)                               |
| `DATABASE_PATH`               | `/data/pricetracker.db` | SQLite database path                                                                                                   |
| `LOCALE`                      | `en`                    | Default locale fallback when Telegram `language_code` missing (`en`, `it`)                                             |
| `LOG_LEVEL`                   | `INFO`                  | structlog log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`)                                                              |
| `PROMETHEUS_BIND`             | `127.0.0.1:9090`        | Prometheus exporter bind address (host:port)                                                                           |
| `CHECK_INTERVAL_MINUTES`      | `360`                   | Default polling interval (per-product overridable via `/set_interval`)                                                 |
| `MAX_CONSECUTIVE_ERRORS`      | `10`                    | Failed checks before a product is auto-suspended (domain-wide quarantine uses the fixed tiers in `core/health.py`, not this variable) |
| `LISTING_GONE_CONFIRMATIONS`  | `3`                     | Consecutive HTTP 404/410 answers before a removed listing is suspended                                                 |
| `REQUEST_TIMEOUT`             | `30`                    | HTTP timeout in seconds for scraper requests                                                                           |
| `NOTIFICATION_COOLDOWN_HOURS` | `24`                    | Per-product alert cooldown                                                                                             |
| `METRICS_ENABLED`             | `true`                  | Prometheus exporter is on by default. Set `0` or `false` to disable.                                                  |

For the full set including outlier detection thresholds and currency tuning, see `src/price_tracker/config.py`.

## Backup & restore

### What to back up

All runtime state lives in **one file**: the SQLite database at `DATABASE_PATH` (default `/data/pricetracker.db`). It contains users, products, price history, scraper health, notification preferences, the digest queue, and the cached currency rates. There are no separate JSON state files.

```bash
# Stop the bot to ensure a consistent snapshot
docker compose stop price-tracker-bot

# Snapshot via SQLite's online backup API (preferred, even when running)
sqlite3 /data/pricetracker.db ".backup '/backups/pricetracker-$(date +%Y%m%d).db'"

# Or simple file copy (must stop the bot first to avoid WAL inconsistency)
cp /data/pricetracker.db /backups/pricetracker-$(date +%Y%m%d).db

docker compose start price-tracker-bot
```

### Restore

```bash
docker compose stop price-tracker-bot
cp /backups/pricetracker-YYYYMMDD.db /data/pricetracker.db
docker compose start price-tracker-bot
```

The migrator at startup is idempotent: if the snapshot was taken on an older schema version, the next startup applies the missing migrations automatically.

Migrations `014` (adds suspension provenance columns) and `015` (rebuilds `digest_queue` with
a nullable `product_id`) ship together with the operational-notices feature. `015` recreates
the `digest_queue` table rather than altering it in place — take a backup before upgrading, as
for every release, and let the idempotent migrator run once at startup.

## Hardened deployment

`docker-compose.yml` ships F7 hardening directives (Plan 3 F7 closure). Reference excerpt:

```yaml
services:
  price-tracker-bot:
    build: .
    user: "1000:1000"
    read_only: true
    tmpfs:
      - /tmp:size=64m,mode=1777
      - /home/botuser/.cache:size=512m,mode=0755,uid=1000,gid=1000
    cap_drop: [ALL]
    security_opt:
      - no-new-privileges:true
    mem_limit: 768m
    mem_reservation: 384m
    cpus: 1.0
    pids_limit: 256
    restart: unless-stopped
    env_file: .env
    volumes:
      - price-tracker-data:/data
```

Notes:
- `read_only: true` makes the root filesystem immutable. Writable areas: `/tmp` (tmpfs 64m) and `/home/botuser/.cache` (tmpfs 512m for Playwright).
- `cap_drop: [ALL]` removes every Linux capability — the bot needs none.
- `no-new-privileges:true` blocks setuid escalation.
- `mem_limit: 768m` + `cpus: 1.0` are the verified working budgets on production.
- The `/data` volume is the only writable persistent path; back up its content (see [Backup & restore](#backup--restore)).

### Bind mount instead of the named volume

`docker-compose.yml` ships a **named volume** (`price-tracker-data`) on purpose: Docker seeds a
named volume with the image's `/data` ownership, so the non-root process (`user: "1000:1000"`)
can create the database on first run. A `./data` bind mount is created root-owned by the engine
and breaks that first startup.

A deployment that already keeps its database in a host directory (for example
`/opt/price-tracker/data`, created before the container was hardened) must keep that bind — but
**must not** edit the tracked compose file for it. Declare it as a local, untracked override:

```yaml
# docker-compose.override.yml — not tracked by git
services:
  price-tracker:
    volumes:
      - ./data:/data:rw
```

The override is a per-machine contract, and losing it is silent: `docker compose up` without it
mounts the named volume, Docker creates it empty, the migrations run against a blank database and
the bot comes back up **with no products and no error in the logs**. Before any deploy on such a
host, verify the resolved configuration rather than grepping the file:

```bash
docker compose config --format json | python -c "import json,sys; \
  print([v for v in json.load(sys.stdin)['services']['price-tracker']['volumes'] \
  if v.get('target') == '/data'])"
```

Expect exactly one mount with `"type": "bind"` and the host path you intend.

## Troubleshooting

| Symptom                                | Diagnosis                                  | Action                                                                                                                                                              |
| -------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Scraper silently stops fetching        | Domain is quarantined by HealthManager     | `/health` shows the domain in `LOCKED` state; logs `quarantine_locked` event with `block_count` and `locked_until`                                                  |
| All scrapers slow                      | Telegram rate limit (429)                  | Built-in tenacity backoff handles automatically; check logs for `tg_429`                                                                                            |
| Currency conversion shows stale rates  | ECB cache TTL not refreshed                | Cache TTL is 24h; force refresh by stopping the bot and removing the cache row: `sqlite3 /data/pricetracker.db "DELETE FROM bot_config WHERE key='fx_rates_eur'"` |
| Playwright fails with cache error      | `/home/botuser/.cache` tmpfs not mounted   | Verify compose `tmpfs` block; check `docker inspect` for the mount                                                                                                  |
| `database is locked` error             | Multiple writers or interrupted WAL        | Stop the bot, run `sqlite3 /data/pricetracker.db "PRAGMA wal_checkpoint(FULL);"`, restart                                                                           |
| Metrics endpoint returns 404           | `PROMETHEUS_BIND` unset or off-localhost   | Confirm env var; default binds to `127.0.0.1:9090` (not reachable from outside container without explicit port mapping)                                             |
| Bot starts but `/start` is silent      | User not in `ALLOWED_USERS`                | Add Telegram user ID to `.env` and restart container                                                                                                                |

## Upgrade procedure

Until Plan 4 publishes the GitHub Container Registry image:

```bash
git pull
docker compose build
docker compose up -d
```

Once the `ghcr.io/bernalli/price-tracker-bot` image is available (Plan 4 F8 milestone):

```bash
docker compose pull
docker compose up -d
```

Schema migrations apply automatically at startup. Roll back by re-deploying a previous tag and restoring the matching DB snapshot.

## Monitoring quick start

The Prometheus exporter binds to `PROMETHEUS_BIND` (default `127.0.0.1:9090`). Verify it is up:

```bash
curl -fsS http://127.0.0.1:9090/metrics | head -20
```

Expected output: `# HELP price_tracker_*` lines with counters and gauges. For the full metrics catalog and Grafana dashboard import, see [observability.md](observability.md).
