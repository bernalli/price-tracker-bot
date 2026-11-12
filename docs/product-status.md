# Product checks and status

A check reports what the bot could establish for the tracked product. A failed,
quarantined, deferred or held check is not a newly accepted price. The product
view can still show the last recorded price; it does not make that old price a
successful new read. Use the check result and `/errori` for details.

## Price and availability

A usable price needs a valid amount and a known currency. Price, currency and
stock evidence must refer to the same eligible product or offer; an unrelated
offer is not a substitute. A currency mismatch or an implausible reading can
prevent acceptance, and some readings require confirmation. See
[scrapers.md](scrapers.md) for the scraper model.

Availability is separate from a price:

- Two explicit sold-out observations confirm unavailability. The evidence is
  persisted, so a restart does not erase the count. Monitoring continues.
- A priced observation with unknown stock can update the accepted price and
  history, but does not confirm availability or reset the sold-out evidence.
  If the product was already confirmed unavailable, this reading produces
  neither a restock notice nor a price-drop alert.
- A confirmed in-stock observation with an accepted price restores availability
  in one check. A restock notice includes the recorded price.

Stock notices apply the product's notification preferences, including mute.
Price alerts also require the product's rule or target to trigger and the
minimum-drop floor to be met. The default floor is both 2% of the old price and
1 unit of the product's currency. Settings → Minimum drop offers off, 1%, 2%
and 5%; percentage presets retain the configured absolute floor, while off
disables both parts. Operators can configure the defaults; see
[operations.md](operations.md#environment-variables).

## Warnings and suspension

Repeated failures can produce a warning and eventually suspend tracking. A
confirmed removed listing requires three consecutive missing-page observations
by default, or the configured `LISTING_GONE_CONFIRMATIONS` count. Evidence includes
HTTP 404/410 responses and recognized store-specific not-found documents, such as
Amazon's not-found page returned with HTTP 2xx. A redirect alone does not establish
that the listing is gone. Another failure threshold
can suspend a product sooner without confirming its removal.

Warnings are combined per user and domain, with each cause on its own 24-hour
persisted cooldown. Suspension notices are grouped by user, domain and cause.
Their actions use the current automatically suspended group for that user,
domain and cause; deletion asks for confirmation. Manually paused products are
excluded. See [notification preferences](notifications.md#operational-notices)
for routing, buttons and older records.

## Delivery and changes in state

Before a status notice is sent, the bot checks that its recorded transition is
still current. An acknowledgement applies only to that transition, so it cannot
clear a newer one. The digest checks current status and mute/quiet settings again
when it is flushed. Status delivery can be retried: if a send succeeds but
the process stops before recording the acknowledgement, a later retry may send
it again (at-least-once delivery). A state change after a delivery attempt has begun cannot recall a
message already being sent.

See [notification preferences](notifications.md) for the commands, settings and
`/digest_now` behavior.
