-- Persist stock evidence and the latest undelivered status transition atomically
-- with its product state. A newer transition supersedes an obsolete notice.
ALTER TABLE products ADD COLUMN sold_out_streak INTEGER NOT NULL DEFAULT 0;
ALTER TABLE products ADD COLUMN pending_notice_kind TEXT;
ALTER TABLE products ADD COLUMN notice_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE products ADD COLUMN notice_attempted_at TEXT;
ALTER TABLE products ADD COLUMN last_check_outcome TEXT;
ALTER TABLE digest_queue ADD COLUMN event_id TEXT;
CREATE UNIQUE INDEX idx_digest_event ON digest_queue(user_id, event_id)
    WHERE event_id IS NOT NULL;
