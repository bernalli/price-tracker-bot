-- 017. Additive only. No comment in this file contains a semicolon.
-- users.min_drop: the user's minimum price drop before a price alert.
-- NULL = the server default (MIN_DROP_PERCENT and MIN_DROP_ABSOLUTE).
-- 'off' lets every drop through, '1' '2' '5' are percentage presets.
-- Values are validated in code, not by CHECK, so a new preset stays additive.
ALTER TABLE users ADD COLUMN min_drop TEXT;
