# Notification preferences

`price-tracker-bot` ships 8 commands that let each user tune how and when they receive price alerts. All preferences are per-user (and optionally per-product), persisted in the SQLite database, and survive restarts.

## Resolution chain

When the alert engine triggers an alert for a user/product pair, the notifier walks this chain to decide whether to send, queue, or drop the alert:

```
1. mute (per-product or "all") — if active and not expired: DROP
2. quiet hours — if alert falls inside the user's quiet window:
   - if digest_mode=on: ENQUEUE in digest_queue (delivered at the first flush
     after the quiet window ends, not merely the next periodic interval)
   - else: DROP silently
3. throttle (sliding window, per user) — if user has reached their hourly cap:
   - if digest_mode=on: ENQUEUE in digest_queue
   - else: DROP silently
4. digest mode — if user has digest_mode=on:
   - ENQUEUE the alert in digest_queue
   - flush at the next periodic interval (default 60 min) or on /digest_now,
     still subject to the quiet-hours hold above
5. immediate dispatch — otherwise: send via TelegramNotifier.send_alert
```

Operational notices (see below) go through the same chain, with one exception: they
ignore mute (step 1 is skipped for them).

Every step records a Prometheus metric. Drops increment `price_tracker_notification_skipped_total{reason="..."}` with one of `mute`, `quiet_hours`, `throttle`, `digest_pending`. Sends increment `price_tracker_notification_sent_total`. INFO-level logs include `user_id` and `product_id` for traceability.

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

- **Examples**: `/digest_mode on 60`, `/digest_mode off`
- **Default interval**: 60 minutes when no value given.
- **Behavior**: alerts arriving in `on` mode are batched in `digest_queue` and flushed periodically. Switching to `off` does NOT auto-flush — pending entries stay until `/digest_now` or the next scheduled flush.

### `/digest_now`
Flush all pending digest entries immediately.

- **Behavior**: empties the user's `digest_queue` rows and sends a single combined message. No-op if queue empty.

### `/quiet_hours <HH:MM-HH:MM>`
Set a daily silent window (timezone-aware).

- **Examples**: `/quiet_hours 22:00-07:00` (overnight), `/quiet_hours 12:00-13:00` (lunch break)
- **Wraparound**: `22:00-07:00` is interpreted as crossing midnight.
- **Disable**: send `/quiet_hours off`. Sending the command with no arguments returns a usage hint but does NOT disable.

### `/timezone <IANA>`
Set the user's timezone for quiet hours and digest scheduling.

- **Examples**: `/timezone Europe/Rome`, `/timezone America/New_York`
- **Default**: `Europe/Rome` (hardcoded; not derived from the server timezone).
- **Validation**: invalid IANA names are rejected with a usage hint.

### `/throttle <max_per_hour>`
Cap the number of alerts the user receives per sliding hour.

- **Examples**: `/throttle 5` (max 5/h), `/throttle off` (disable throttle, unlimited)
- **Behavior**: a sliding 60-minute window of recent sends is kept per user. When the window is full, further alerts are dropped (logged as `throttled`, counted in metrics) until older entries fall out.

### `/prefs [product_id]`
Show the user's current preferences in a single message.

- **Optional argument**: `product_id` — when provided, the output includes the per-product mute state for that specific product alongside the user-level prefs.
- **Output includes**: digest mode + interval, quiet hours window, timezone, throttle cap, current mutes (per-product list + global), recent throttle window status.

## Defaults for new users

A first-time `/start` user is created with these defaults (until they explicitly change anything):

| Preference        | Default              |
| ----------------- | -------------------- |
| Mute              | none                 |
| Digest mode       | off (immediate send) |
| Digest interval   | 60 min (when enabled)|
| Quiet hours       | none                 |
| Timezone          | `Europe/Rome`        |
| Throttle          | unlimited (stored as `NULL`) |
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

## Operational notices

Besides price-drop alerts, the bot sends **operational notices**: automatic reports about a
product's *tracking health* rather than its price — the listing was removed, the price could
no longer be read, the site stopped answering, the site is blocking automated checks, or a
site-wide quarantine. A pre-suspension warning fires at half the failure threshold, before the
product is actually suspended.

### Aggregation

Notices are grouped **per user and per domain** (the eTLD+1 that groups Shopify stores,
subdomains, etc. together) and batched during a sweep: a store that goes down taking twenty
tracked products with it produces **one** message per user for that domain, not twenty. Each
message lists up to 10 affected products (`… and {k} more` beyond that), with the reason,
the last good read (price and date) if any, and the raw error truncated to a fixed budget.

### Routing: mute is ignored, only global preferences apply

Operational notices route with `kind="operational"` and always resolve preferences through
`PreferencesManager.resolve_global(user_id)` — the user's **global** row only, never a
per-product override. Two consequences:

- **`/mute` has no effect on them.** A muted product can still trigger an operational notice
  for its domain; mute only suppresses price-drop alerts. There is no separate command to
  silence operational notices.
- **Quiet hours, digest mode and throttle apply exactly as for price alerts** (see the
  resolution chain above), using the user's account-wide settings — never a per-product
  override, since a notice can cover several products from several owners' worth of
  preferences in principle, but always resolves to the domain-group's single recipient.

When an operational notice is deferred (quiet hours, throttle, or digest mode), it is queued
with `product_id = NULL` rather than tied to one of the affected products: deleting any single
product in the group does not drop the others' pending notice.

### Buttons

Suspension notices carry two buttons, `▶️ Reactivate and recheck (N)` and `🗑 Delete all (N)`
(button order depends on the reason — deletion comes first when the listing is confirmed
gone). Deleting asks for a `🗑 Yes, delete N` / `❌ Cancel` confirmation. Both actions apply to
the **whole domain group** for that user, computed from persisted state at click time (see
provenance below) — not from whatever the current failure threshold happens to be. The
pre-suspension warning carries no buttons; use `/reactivate` or `/errori` for those.

### `LISTING_GONE_CONFIRMATIONS`

A listing is only ever confirmed *gone* — as opposed to merely unreachable or unreadable —
after `LISTING_GONE_CONFIRMATIONS` (default `3`) consecutive HTTP 404/410 responses. Any
other outcome in between (success or a different failure) resets the streak. See
[operations.md](operations.md#environment-variables) for the environment variable.

### Suspension provenance and pre-migration rows

Every automatic suspension records `suspension_kind = 'automatic'` and the `suspension_reason`
that caused it, distinct from a manual `/pause` (`suspension_kind = 'manual'`). The group
buttons act only on rows with `suspension_kind = 'automatic'` for that domain: a product a
user paused by hand is never swept up by "Delete all". Rows suspended **before** this
provenance column existed have `suspension_kind IS NULL` — their origin cannot be
reconstructed, so they are excluded from the group entirely; reactivating or deleting them
goes through `/reactivate` one product at a time, same as before this feature.

### Length limits and segmentation

Every field is truncated to a fixed visible-character budget before escaping (name, domain,
last error, the human-readable reason), and at most 10 products are listed per message. Any
message that still exceeds Telegram's length limit is split across multiple messages on line
boundaries, with the inline keyboard attached to the last chunk — the same segmentation
contract used for digest pages and button responses (`core/textlimits.py`).

## Related docs

- [architecture.md](architecture.md) — where the notifier sits in the data flow.
- [operations.md](operations.md) — backup/restore and troubleshooting.
- [observability.md](observability.md) — alert metrics and dashboard panels.
