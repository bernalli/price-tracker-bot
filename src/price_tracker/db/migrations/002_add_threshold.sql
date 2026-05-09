ALTER TABLE products ADD COLUMN threshold_type TEXT NOT NULL DEFAULT 'percentage';
ALTER TABLE products ADD COLUMN threshold_value TEXT NOT NULL DEFAULT '10';
ALTER TABLE products ADD COLUMN target_price TEXT;
ALTER TABLE products ADD COLUMN domain TEXT;
ALTER TABLE products ADD COLUMN lowest_price TEXT;
ALTER TABLE products ADD COLUMN highest_price TEXT;
ALTER TABLE products ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_products_active ON products(is_active);
CREATE INDEX IF NOT EXISTS idx_products_user ON products(user_id, is_active);
