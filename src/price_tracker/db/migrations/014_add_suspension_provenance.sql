-- Consecutive 404/410 answers on the product page. Reset by any other
-- outcome (a success or a different failure). Drives the early suspension
-- of listings that were removed from the catalog.
ALTER TABLE products ADD COLUMN gone_streak INTEGER NOT NULL DEFAULT 0;
-- Who paused the product: the user ('manual') or the scheduler ('automatic').
-- Written by the code path that pauses, never inferred from the counters:
-- a manual pause after a run of failures looks identical to an automatic
-- one otherwise. NULL on rows paused before this migration: origin unknown,
-- treated as manual by every bulk action.
ALTER TABLE products ADD COLUMN suspension_kind TEXT CHECK (suspension_kind IN ('manual', 'automatic'));
ALTER TABLE products ADD COLUMN suspension_reason TEXT;
