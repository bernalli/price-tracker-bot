CREATE TABLE IF NOT EXISTS scraper_health (
    domain TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'CLOSED'
        CHECK(state IN (
            'CLOSED','LOCKED_T1','LOCKED_T2','LOCKED_T3',
            'HALF_OPEN_T1','HALF_OPEN_T2','HALF_OPEN_T3'
        )),
    consecutive_blocks INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMP,
    last_block_at TIMESTAMP,
    last_block_reason TEXT,
    last_success_at TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_scraper_health_locked_until
    ON scraper_health(locked_until) WHERE locked_until IS NOT NULL;
