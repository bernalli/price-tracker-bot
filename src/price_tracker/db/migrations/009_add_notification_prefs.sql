CREATE TABLE IF NOT EXISTS notification_prefs (
    user_id INTEGER NOT NULL,
    product_id INTEGER,
    mute INTEGER NOT NULL DEFAULT 0 CHECK(mute IN (0,1)),
    mute_until TIMESTAMP,
    digest_mode INTEGER NOT NULL DEFAULT 0 CHECK(digest_mode IN (0,1)),
    digest_interval_minutes INTEGER DEFAULT 60 CHECK(digest_interval_minutes > 0),
    quiet_hours_start TEXT,
    quiet_hours_end TEXT,
    throttle_per_hour INTEGER CHECK(throttle_per_hour IS NULL OR throttle_per_hour > 0),
    timezone TEXT NOT NULL DEFAULT 'Europe/Rome',
    throttle_state_json TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, product_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_notif_prefs_user ON notification_prefs(user_id);
