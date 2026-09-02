-- An aggregated operational notice belongs to a user and a domain, not to
-- one product: with product_id NOT NULL + ON DELETE CASCADE, deleting the
-- product a queued notice happened to reference dropped the notice for every
-- other product in the group. Rebuild with a nullable reference that is
-- cleared, not cascaded.
CREATE TABLE digest_queue_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    product_id INTEGER,
    alert_payload_json TEXT NOT NULL,
    enqueued_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    flushed_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL
);
INSERT INTO digest_queue_v2 (id, user_id, product_id, alert_payload_json, enqueued_at, flushed_at)
    SELECT id, user_id, product_id, alert_payload_json, enqueued_at, flushed_at FROM digest_queue;
DROP INDEX IF EXISTS idx_digest_pending;
DROP TABLE digest_queue;
ALTER TABLE digest_queue_v2 RENAME TO digest_queue;
CREATE INDEX IF NOT EXISTS idx_digest_pending
    ON digest_queue(user_id, flushed_at) WHERE flushed_at IS NULL;
