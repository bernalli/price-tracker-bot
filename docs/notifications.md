# Notification preferences

`price-tracker-bot` ships 8 commands that let each user tune how and when they receive price alerts. All preferences are per-user (and optionally per-product), persisted in the SQLite database, and survive restarts.

## Resolution chain

When the alert engine triggers an alert for a user/product pair, the notifier walks this chain to decide whether to send, queue, or drop the alert:

```
1. mute (per-product or "all") — if active and not expired: DROP
2. quiet hours — if alert falls inside the user's quiet window: QUEUE for next allowed time
3. throttle (sliding window, per user) — if user has reached their hourly cap: DROP (with warning)
4. digest mode — if user has digest_mode=on:
   - QUEUE the alert in digest_queue
   - flush at the next periodic interval (default 30 min) or on /digest_now
5. immediate dispatch — otherwise: send via TelegramNotifier.send_alert
```

Every step records a metric (`alerts_skipped_total{reason="muted|quiet|throttled|queued"}` or `alerts_sent_total`). Logged at INFO with the user_id and product_id for traceability.

## Commands

### `/mute [product_id|all] [hours|forever]`
Silence alerts for a single product or all products.

- **Default**: `/mute all 24` (24 hours, all products).
- **Examples**:
  - `/mute 42` — mute product 42 for 24 hours
  - `/mute all 168` — mute everything for a week
  - `/mute 42 forever` — mute product 42 indefinitely
- **Behavior**: writes to `notification_prefs` (per-user) or sets a per-product expiry. Active mutes are checked at dispatch time; expired mutes auto-clear.

### `/unmute [product_id|all]`
Remove an active mute.

- **Examples**: `/unmute 42`, `/unmute all`
- **Behavior**: clears the mute entry; alerts resume immediately for that target.

### `/digest_mode <on|off> [interval_min]`
Switch between immediate and digest delivery.

- **Examples**: `/digest_mode on 30`, `/digest_mode off`
- **Default interval**: 30 minutes when no value given.
- **Behavior**: alerts arriving in `on` mode are batched in `digest_queue` and flushed periodically. Switching to `off` does NOT auto-flush — pending entries stay until `/digest_now` or the next scheduled flush.

### `/digest_now`
Flush all pending digest entries immediately.

- **Behavior**: empties the user's `digest_queue` rows and sends a single combined message. No-op if queue empty.

### `/quiet_hours <HH:MM-HH:MM>`
Set a daily silent window (timezone-aware).

- **Examples**: `/quiet_hours 22:00-07:00` (overnight), `/quiet_hours 12:00-13:00` (lunch break)
- **Wraparound**: `22:00-07:00` is interpreted as crossing midnight.
- **Disable**: send `/quiet_hours` with no args (or any input that fails parsing) — the prompt explains the syntax.

### `/timezone <IANA>`
Set the user's timezone for quiet hours and digest scheduling.

- **Examples**: `/timezone Europe/Rome`, `/timezone America/New_York`
- **Default**: server timezone (typically UTC).
- **Validation**: invalid IANA names are rejected with a usage hint.

### `/throttle <max_per_hour>`
Cap the number of alerts the user receives per sliding hour.

- **Examples**: `/throttle 5` (max 5/h), `/throttle 0` (disable throttle, unlimited)
- **Behavior**: a sliding 60-minute window of recent sends is kept per user. When the window is full, further alerts are dropped (logged as `throttled`, counted in metrics) until older entries fall out.

### `/prefs`
Show the user's current preferences in a single message.

- **Output includes**: digest mode + interval, quiet hours window, timezone, throttle cap, current mutes (per-product list + global), recent throttle window status.
- **No arguments**.

## Defaults for new users

A first-time `/start` user is created with these defaults (until they explicitly change anything):

| Preference        | Default              |
| ----------------- | -------------------- |
| Mute              | none                 |
| Digest mode       | off (immediate send) |
| Digest interval   | 30 min (when enabled)|
| Quiet hours       | none                 |
| Timezone          | server (UTC if unset)|
| Throttle          | 0 (unlimited)        |
| Notification mode | immediate            |

## Persistence

All preferences are stored in the SQLite database at `DATABASE_PATH` — there are no separate JSON state files.

| What                          | Storage                                                  |
| ----------------------------- | -------------------------------------------------------- |
| Mute / digest / quiet / timezone / throttle config | `notification_prefs` table (one row per user) |
| Throttle sliding window state | `notification_prefs.throttle_state_json` column (JSON encoded list of recent send timestamps) |
| Digest queue (pending alerts) | `digest_queue` table (one row per pending alert)         |

The migrator handles schema upgrades automatically (`db/migrations/009_add_notification_prefs.sql` adds the prefs table; `010_add_digest_queue.sql` adds the queue table). Backups are a single SQLite file — see [operations.md#backup--restore](operations.md#backup--restore).

## Resolution priority

When determining the effective preference for a given alert, the notifier resolves in this order (most specific wins):

1. **Per-product mute** — if the user has muted the specific product, that wins.
2. **Global "all" mute** — if the user has `/mute all` active, applies to every product.
3. **User-level prefs** — digest mode, quiet hours, throttle, timezone (no per-product override; these are user-wide).
4. **Defaults** — applied for any unset field.

The `EffectivePrefs` dataclass (`notifier/preferences.py:21`) is the resolved snapshot used at dispatch time, computed by `PreferencesManager.resolve(*, user_id, product_id)` (`preferences.py:68`).

## Related docs

- [architecture.md](architecture.md) — where the notifier sits in the data flow.
- [operations.md](operations.md) — backup/restore and troubleshooting.
- [observability.md](observability.md) — alert metrics and dashboard panels.
