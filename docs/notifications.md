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

Operational notices (see below) ignore mute. During quiet hours or throttling they
are queued even when digest mode is off. Stock notices are per-product and honor mute.
At digest delivery, current status and mute/quiet settings are checked again.

Notification routing records Prometheus metrics, including
`price_tracker_notification_skipped_total{reason="..."}` and
`price_tracker_notification_sent_total`.

## Commands

### `/mute [product_id|all] [hours|forever]`
Silence alerts for a single product or all products.

- **Default**: `/mute all 24` (24 hours, all products).
- **Examples**:
  - `/mute 42` — mute product 42 for 24 hours
  - `/mute all 168` — mute everything for a week
  - `/mute 42 forever` — mute product 42 indefinitely
- **Behavior**: writes to `notification_prefs` (per-user) or sets a per-product expiry. Active mutes are checked at dispatch time; expired mutes no longer suppress alerts.

### `/unmute [product_id|all]`
Remove an active mute.

- **Examples**: `/unmute 42`, `/unmute all`
- **Behavior**: clears the mute of that target only. `/unmute 42` clears the mute of product 42;
  while everything is muted (`/mute all`), product 42 stays muted too. `/unmute all` clears the
  global mute; a product muted on its own stays muted until its own mute ends.

### `/digest_mode <on|off> [interval_min]`
Switch between immediate and digest delivery.

- **Examples**: `/digest_mode on 60`, `/digest_mode off`
- **Default interval**: 60 minutes when no value given.
- **Behavior**: alerts arriving in `on` mode are batched in `digest_queue` and flushed periodically. Switching to `off` does NOT auto-flush — pending entries stay until `/digest_now` or the next scheduled flush.

### `/digest_now`
Attempt to flush eligible pending digest entries.

- **Behavior**: current status and notification settings are checked again. Quiet hours or
  mute can leave eligible entries queued; obsolete status entries are retired. Messages can
  span multiple Telegram pages. The response reports the number processed and the number
  still queued. Processed includes unreadable entries quarantined by the digest service,
  so it does not mean that every counted item was sent; obsolete or currently ineligible
  entries are excluded from that count.

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
- **Behavior**: a sliding 60-minute window of recent sends is kept per user. When the window is full, further price and stock alerts are queued if digest mode is on, or dropped otherwise, until older entries fall out. Operational notices are queued.

### `/prefs [product_id]`
Show the user's current preferences in a single message.

- **Optional argument**: `product_id` — when provided, the output includes the per-product mute state for that specific product alongside the user-level prefs.
- **Output includes**: the selected scope, its effective mute, digest mode + interval, quiet hours, throttle cap and timezone.

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
| Mute / digest / quiet / timezone / throttle config | `notification_prefs` table (global and per-product rows) |
| Throttle sliding window state | `notification_prefs.throttle_state_json` column (JSON encoded list of recent send timestamps) |
| Digest queue (pending alerts) | `digest_queue` table (one row per pending alert)         |

The migrator handles schema upgrades automatically (`db/migrations/009_add_notification_prefs.sql` adds the prefs table; `010_add_digest_queue.sql` adds the queue table). Backups are a single SQLite file — see [operations.md#backup--restore](operations.md#backup--restore).

## Resolution priority

When determining the effective preference for a given alert, the notifier resolves in this order:

1. **Mute** — the product's own mute and the global "all" mute add up: the product is muted
   while either one is active, until the later of the two ends, and a mute without an end
   wins. Unmuting a product clears only its own mute; it never exempts the product from
   `/mute all`.
2. **User-level prefs** — digest mode, quiet hours, throttle, timezone (no per-product override; these are user-wide).
3. **Defaults** — applied for any unset field.

The `EffectivePrefs` dataclass in `notifier/preferences.py` is the resolved snapshot used at dispatch time, computed by `PreferencesManager.resolve(*, user_id, product_id)`.

## Operational notices

Besides price-drop alerts, the bot sends **operational notices**: automatic reports about a
product's *tracking health* rather than its price — the listing was removed, the price could
no longer be read, the site stopped answering, the site is blocking automated checks, or a
site-wide quarantine. A pre-suspension warning fires at half the failure threshold, before the
product is actually suspended. A site that blocks automated checks is reported by the site-wide
quarantine notice alone: a block does not count toward the failure threshold, so it produces
neither a pre-suspension warning nor a suspension.

### Aggregation

Warnings are grouped **per user and per domain** during a sweep, including different
causes in one warning. Up to 10 affected products are shown (`… and {k} more` beyond
that), with their IDs, reasons, failure counters and last recorded prices when available.
Each warning cause has its own 24-hour cooldown per user and domain, persisted across
restarts after the notice is sent or queued. Suspensions are grouped by user, domain and
cause. Stock transitions remain individual so each product's preferences apply. The
domain key uses eTLD+1 where available.

### Routing: mute is ignored, only global preferences apply

Operational notices route with `kind="operational"` and always resolve preferences through
`PreferencesManager.resolve_global(user_id)` — the user's **global** row only, never a
per-product override. Two consequences:

- **`/mute` has no effect on operational notices.** A muted product can still trigger
  one for its domain. There is no separate command to silence operational notices.
- **Quiet hours, digest mode and throttle use account-wide settings.** Operational
  notices held by quiet hours or throttling are queued even with digest mode off.
  Stock notifications use the product's effective preferences and honor mute.

When an operational notice is deferred (quiet hours, throttle, or digest mode), it is queued
with `product_id = NULL` rather than tied to one of the affected products: deleting any single
product in the group does not drop the others' pending notice.

### Buttons

Suspension notices carry two buttons, `▶️ Reactivate and recheck (N)` and `🗑 Delete all (N)`
(button order depends on the reason — deletion comes first when the listing is confirmed
gone). Deleting asks for a `🗑 Yes, delete N` / `❌ Cancel` confirmation. Both actions apply to
the current automatically suspended products for that user, domain and suspension cause,
computed from persisted state at click time (see provenance below). Membership can change
between the message and the click; deletion recomputes it again at confirmation. The
pre-suspension warning carries no buttons; use `/reactivate` or `/errori` for those.

### `LISTING_GONE_CONFIRMATIONS`

A listing is only ever confirmed *gone* — as opposed to merely unreachable or unreadable —
after `LISTING_GONE_CONFIRMATIONS` (default `3`) consecutive confirmed missing-page
observations. These include HTTP 404/410 responses and a recognized store-specific
not-found document, such as Amazon's not-found page even when returned with HTTP 2xx.
A redirect alone is not proof. A success or a different failure resets the streak. A
separate failure threshold can suspend tracking earlier without confirming
the listing gone. See
[operations.md](operations.md#environment-variables) for the environment variable.

### Suspension provenance and pre-migration rows

Every automatic suspension records `suspension_kind = 'automatic'` and the `suspension_reason`
that caused it, distinct from a manual `/pause` (`suspension_kind = 'manual'`). The group
buttons act only on rows with `suspension_kind = 'automatic'` for that user, domain and
the anchor product's suspension cause: a product paused by hand is never swept up by "Delete all". Rows suspended **before** this
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

- [product-status.md](product-status.md) — how check results and availability are confirmed.
- [architecture.md](architecture.md) — where the notifier sits in the data flow.
- [operations.md](operations.md) — backup/restore and troubleshooting.
- [observability.md](observability.md) — alert metrics and dashboard panels.
