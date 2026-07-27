-- Held-read state for the two-read confirmation gate.
-- An implausible scrape (steep drop or sharp rise vs recent history) is parked
-- here instead of being persisted, and is only trusted once the next check
-- reports an agreeing price. Distinct from pending_alert_*, which tracks the
-- last alert already sent for anti-flap cooldown.
ALTER TABLE products ADD COLUMN pending_read_price TEXT DEFAULT NULL;
ALTER TABLE products ADD COLUMN pending_read_count INTEGER NOT NULL DEFAULT 0;
-- Consecutive held reads regardless of whether they agreed with each other.
-- Bounds the gate: a product whose new price is real but too jittery to confirm
-- would otherwise be frozen on a stale price forever.
ALTER TABLE products ADD COLUMN pending_read_streak INTEGER NOT NULL DEFAULT 0;
