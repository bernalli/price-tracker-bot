-- 018. Additive only. No comment in this file contains a semicolon.
-- products.image_url: the product photo URL a scraper found on the product page.
-- This CHECK enforces storage type, prefix, length and absence of NUL.
-- URL semantics are validated by the application before use.
-- NULL = no image known.
ALTER TABLE products ADD COLUMN image_url TEXT CHECK (image_url IS NULL OR (typeof(image_url) = 'text' AND instr(image_url, char(0)) = 0 AND length(image_url) BETWEEN 9 AND 2048 AND substr(image_url, 1, 8) = 'https://'));
