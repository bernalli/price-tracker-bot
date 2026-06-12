DELETE FROM notification_prefs
WHERE product_id IS NULL
  AND EXISTS (
    SELECT 1 FROM notification_prefs AS newer
    WHERE newer.product_id IS NULL
      AND newer.user_id = notification_prefs.user_id
      AND (newer.updated_at > notification_prefs.updated_at
           OR (newer.updated_at = notification_prefs.updated_at
               AND newer.rowid > notification_prefs.rowid))
  );

CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_prefs_global
    ON notification_prefs(user_id) WHERE product_id IS NULL;
