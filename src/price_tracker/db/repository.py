"""SQLite repository — typed CRUD over schema applied by migrator."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from price_tracker.db.models import (
    DigestEntry,
    NotificationPrefs,
    PriceHistoryRecord,
    ProductErrorRow,
    ProductRecord,
    ScraperHealth,
    UserRecord,
)

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-format timestamp from DB into a timezone-aware datetime.

    SQLite CURRENT_TIMESTAMP writes naive strings ('2026-05-09 20:30:00').
    Fields written via .isoformat() from UTC-aware datetimes are already aware.
    Always attaches UTC when the parsed value is naive so callers receive a
    consistent type and comparisons never raise TypeError.
    """
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _dec(value: object) -> Decimal | None:
    """Safely convert a DB value to Decimal."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def _dec_str(value: Decimal | None) -> str | None:
    """Convert Decimal to string for DB storage."""
    return str(value) if value is not None else None


_PRODUCT_COLS = (
    "id, user_id, url, name, domain, initial_price, current_price, "
    "lowest_price, highest_price, target_price, threshold_type, "
    "threshold_value, is_active, is_available, consecutive_errors, "
    "currency, check_interval_minutes, last_checked_at, last_notified_at, "
    "pending_alert_price, pending_alert_at, preferred_condition, preferred_seller, "
    "pending_read_price, pending_read_count, pending_read_streak, last_error, "
    "last_error_at, gone_streak, suspension_kind, suspension_reason"
)


def _row_to_product(row: tuple[Any, ...]) -> ProductRecord:
    # Default only on NULL — a stored 0 (e.g. 0% threshold / any_drop) is valid
    # and must not be coalesced away by a falsy `or` (#24).
    threshold_value = _dec(row[11])
    if threshold_value is None:
        threshold_value = Decimal("10")
    return ProductRecord(
        id=row[0],
        user_id=row[1],
        url=row[2],
        name=row[3],
        domain=row[4],
        initial_price=_dec(row[5]),
        current_price=_dec(row[6]),
        lowest_price=_dec(row[7]),
        highest_price=_dec(row[8]),
        target_price=_dec(row[9]),
        threshold_type=row[10],
        threshold_value=threshold_value,
        is_active=bool(row[12]),
        is_available=bool(row[13]),
        consecutive_errors=int(row[14]),
        currency=row[15],
        check_interval_minutes=row[16],
        last_checked_at=row[17],
        last_notified_at=row[18],
        pending_alert_price=_dec(row[19]),
        pending_alert_at=row[20],
        preferred_condition=row[21],
        preferred_seller=row[22],
        pending_read_price=_dec(row[23]),
        pending_read_count=int(row[24] or 0),
        pending_read_streak=int(row[25] or 0),
        last_error=row[26],
        last_error_at=row[27],
        gone_streak=int(row[28] or 0),
        suspension_kind=row[29],
        suspension_reason=row[30],
    )


class Repository:
    """Typed CRUD wrapper over the SQLite connection."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    # ── Config ─────────────────────────────────────────────────

    async def get_config(self, key: str) -> str | None:
        cursor = await self._conn.execute("SELECT value FROM bot_config WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_config(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT INTO bot_config(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._conn.commit()

    # ── Users ──────────────────────────────────────────────────

    async def ensure_user(self, user_id: int, *, is_admin: bool = False) -> None:
        await self._conn.execute(
            "INSERT INTO users(user_id, is_admin) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET is_admin = excluded.is_admin",
            (user_id, 1 if is_admin else 0),
        )
        await self._conn.commit()

    async def is_user_allowed(self, user_id: int) -> bool:
        cursor = await self._conn.execute(
            "SELECT is_active FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return bool(row and row[0])

    async def get_user(self, user_id: int) -> UserRecord | None:
        cursor = await self._conn.execute(
            "SELECT user_id, is_admin, is_active, display_name, username "
            "FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return UserRecord(
            user_id=row[0],
            is_admin=bool(row[1]),
            is_active=bool(row[2]),
            display_name=row[3],
            username=row[4],
        )

    async def update_user_info(
        self,
        user_id: int,
        *,
        display_name: str | None = None,
        username: str | None = None,
    ) -> None:
        await self._conn.execute(
            "UPDATE users SET display_name = COALESCE(?, display_name), "
            "username = COALESCE(?, username) WHERE user_id = ?",
            (display_name, username, user_id),
        )
        await self._conn.commit()

    async def set_admin(self, user_id: int, is_admin: bool) -> None:
        await self._conn.execute(
            "UPDATE users SET is_admin = ? WHERE user_id = ?",
            (1 if is_admin else 0, user_id),
        )
        await self._conn.commit()

    async def remove_user(self, user_id: int) -> None:
        await self._conn.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))
        await self._conn.commit()

    async def list_users(self) -> list[UserRecord]:
        cursor = await self._conn.execute(
            "SELECT user_id, is_admin, is_active, display_name, username FROM users"
        )
        rows = await cursor.fetchall()
        return [
            UserRecord(
                user_id=r[0],
                is_admin=bool(r[1]),
                is_active=bool(r[2]),
                display_name=r[3],
                username=r[4],
            )
            for r in rows
        ]

    async def list_active_users(self) -> list[UserRecord]:
        cursor = await self._conn.execute(
            "SELECT user_id, is_admin, is_active, display_name, username "
            "FROM users WHERE is_active = 1"
        )
        rows = await cursor.fetchall()
        return [
            UserRecord(
                user_id=r[0],
                is_admin=bool(r[1]),
                is_active=bool(r[2]),
                display_name=r[3],
                username=r[4],
            )
            for r in rows
        ]

    async def ensure_admin_users(self, user_ids: tuple[int, ...]) -> None:
        for uid in user_ids:
            await self.ensure_user(user_id=uid, is_admin=True)

    # ── Products ───────────────────────────────────────────────

    async def add_product(
        self,
        *,
        user_id: int,
        url: str,
        name: str | None,
        domain: str | None,
        initial_price: Decimal | None,
        currency: str,
        threshold_type: str = "percentage",
        threshold_value: Decimal = Decimal("10"),
    ) -> int:
        cursor = await self._conn.execute(
            "INSERT INTO products(user_id, url, name, domain, initial_price, "
            "current_price, lowest_price, highest_price, currency, "
            "threshold_type, threshold_value) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                url,
                name,
                domain,
                _dec_str(initial_price),
                _dec_str(initial_price),
                _dec_str(initial_price),
                _dec_str(initial_price),
                currency,
                threshold_type,
                _dec_str(threshold_value),
            ),
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    async def get_product(self, product_id: int) -> ProductRecord | None:
        cursor = await self._conn.execute(
            f"SELECT {_PRODUCT_COLS} FROM products WHERE id = ?",
            (product_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_product(tuple(row))

    async def list_products_for_user(
        self, *, user_id: int, only_active: bool = False
    ) -> list[ProductRecord]:
        sql = f"SELECT {_PRODUCT_COLS} FROM products WHERE user_id = ?"
        params: tuple[Any, ...] = (user_id,)
        if only_active:
            sql += " AND is_active = 1"
        sql += " ORDER BY id ASC"
        cursor = await self._conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [_row_to_product(tuple(r)) for r in rows]

    async def delete_product(self, product_id: int, *, user_id: int) -> bool:
        cursor = await self._conn.execute(
            "DELETE FROM products WHERE id = ? AND user_id = ?",
            (product_id, user_id),
        )
        await self._conn.commit()
        return int(cursor.rowcount) > 0

    async def update_price(self, product_id: int, price: Decimal) -> None:
        await self._conn.execute(
            "UPDATE products SET current_price = ?, "
            "lowest_price = CASE WHEN lowest_price IS NULL OR "
            "CAST(? AS REAL) < CAST(lowest_price AS REAL) THEN ? ELSE lowest_price END, "
            "highest_price = CASE WHEN highest_price IS NULL OR "
            "CAST(? AS REAL) > CAST(highest_price AS REAL) THEN ? ELSE highest_price END, "
            "last_checked_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
            (
                _dec_str(price),
                _dec_str(price),
                _dec_str(price),
                _dec_str(price),
                _dec_str(price),
                product_id,
            ),
        )
        await self._conn.commit()

    async def set_threshold(
        self, product_id: int, threshold_type: str, threshold_value: Decimal
    ) -> None:
        await self._conn.execute(
            "UPDATE products SET threshold_type = ?, threshold_value = ? WHERE id = ?",
            (threshold_type, _dec_str(threshold_value), product_id),
        )
        await self._conn.commit()

    async def set_target_price(self, product_id: int, target: Decimal | None) -> None:
        await self._conn.execute(
            "UPDATE products SET target_price = ? WHERE id = ?",
            (_dec_str(target), product_id),
        )
        await self._conn.commit()

    async def set_check_interval(self, product_id: int, minutes: int | None) -> None:
        await self._conn.execute(
            "UPDATE products SET check_interval_minutes = ? WHERE id = ?",
            (minutes, product_id),
        )
        await self._conn.commit()

    async def pause_product(self, product_id: int) -> None:
        await self._conn.execute(
            "UPDATE products SET is_active = 0, suspension_kind = 'manual', "
            "suspension_reason = NULL, updated_at = datetime('now') WHERE id = ?",
            (product_id,),
        )
        await self._conn.commit()

    async def reactivate_product(self, product_id: int) -> None:
        await self._conn.execute(
            "UPDATE products SET is_active = 1, consecutive_errors = 0, gone_streak = 0, "
            "suspension_kind = NULL, suspension_reason = NULL WHERE id = ?",
            (product_id,),
        )
        await self._conn.commit()

    async def increment_errors(self, product_id: int) -> None:
        await self._conn.execute(
            "UPDATE products SET consecutive_errors = consecutive_errors + 1 WHERE id = ?",
            (product_id,),
        )
        await self._conn.commit()

    async def reset_errors(self, product_id: int) -> None:
        await self._conn.execute(
            "UPDATE products SET consecutive_errors = 0, gone_streak = 0 WHERE id = ?",
            (product_id,),
        )
        await self._conn.commit()

    async def record_failure(
        self, product_id: int, *, reason: str, detail: str | None = None
    ) -> ProductRecord | None:
        """Count one failed check in a single statement and return the fresh row.

        ``gone_streak`` grows only on ``listing_gone`` and resets on any other
        reason; ``last_error`` keeps the ``"{reason}: {detail}"`` shape /errori shows.
        """
        text = f"{reason}: {detail}" if detail else reason
        await self._conn.execute(
            "UPDATE products SET consecutive_errors = consecutive_errors + 1, "
            "gone_streak = CASE WHEN ? = 'listing_gone' THEN gone_streak + 1 ELSE 0 END, "
            "last_error = ?, last_error_at = datetime('now') WHERE id = ?",
            (reason, text[:300], product_id),
        )
        await self._conn.commit()
        return await self.get_product(product_id)

    async def suspend_product(self, product_id: int, *, reason: str) -> bool:
        cursor = await self._conn.execute(
            "UPDATE products SET is_active = 0, suspension_kind = 'automatic', "
            "suspension_reason = ?, updated_at = datetime('now') "
            "WHERE id = ? AND is_active = 1",
            (reason, product_id),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def list_auto_suspended_products(self, *, user_id: int) -> list[ProductRecord]:
        cursor = await self._conn.execute(
            f"SELECT {_PRODUCT_COLS} FROM products "
            "WHERE user_id = ? AND is_active = 0 AND suspension_kind = 'automatic' "
            "ORDER BY id ASC",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_product(tuple(r)) for r in rows]

    async def set_last_error(self, product_id: int, error_text: str) -> None:
        """Persist the most recent scrape failure reason for /errori visibility."""
        await self._conn.execute(
            "UPDATE products SET last_error = ?, last_error_at = datetime('now') WHERE id = ?",
            (error_text[:300], product_id),
        )
        await self._conn.commit()

    async def list_products_with_errors(self, *, user_id: int) -> list[ProductErrorRow]:
        """Active or paused products of a user that currently carry scrape errors."""
        cursor = await self._conn.execute(
            "SELECT id, name, url, domain, consecutive_errors, last_error, last_error_at "
            "FROM products WHERE user_id = ? AND consecutive_errors > 0 "
            "ORDER BY consecutive_errors DESC, id ASC",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [
            ProductErrorRow(
                id=r[0],
                name=r[1],
                url=r[2],
                domain=r[3],
                consecutive_errors=int(r[4]),
                last_error=r[5],
                last_error_at=r[6],
            )
            for r in rows
        ]

    async def mark_pending_alert(self, product_id: int, price: Decimal) -> None:
        await self._conn.execute(
            "UPDATE products SET pending_alert_price = ?, "
            "pending_alert_at = datetime('now') WHERE id = ?",
            (_dec_str(price), product_id),
        )
        await self._conn.commit()

    async def clear_pending_alert(self, product_id: int) -> None:
        await self._conn.execute(
            "UPDATE products SET pending_alert_price = NULL, pending_alert_at = NULL WHERE id = ?",
            (product_id,),
        )
        await self._conn.commit()

    async def record_alert_sent(self, product_id: int, price: Decimal) -> None:
        """Persist that a price-drop alert was pushed for ``product_id`` at ``price``.

        Anchors the anti-flap dedup used by the scheduler push path:
        ``last_notified_at`` is the cooldown reference and ``pending_alert_price``
        records the alerted price (the episode low-watermark used to detect a new
        low). ``pending_alert_at`` mirrors the timestamp for observability.
        """
        await self._conn.execute(
            "UPDATE products SET last_notified_at = datetime('now'), "
            "pending_alert_price = ?, pending_alert_at = datetime('now') WHERE id = ?",
            (_dec_str(price), product_id),
        )
        await self._conn.commit()

    async def set_availability(self, product_id: int, *, available: bool) -> None:
        """Record whether the listing is currently purchasable."""
        await self._conn.execute(
            "UPDATE products SET is_available = ?, updated_at = datetime('now') WHERE id = ?",
            (1 if available else 0, product_id),
        )
        await self._conn.commit()

    # ── Held reads (two-read confirmation gate) ────────────────

    async def set_pending_read(
        self, product_id: int, price: Decimal, count: int, streak: int
    ) -> None:
        """Park an implausible read until the next check either confirms or drops it.

        Unrelated to ``mark_pending_alert``: this is a read the pipeline has
        refused to trust yet, not an alert already sent.
        """
        await self._conn.execute(
            "UPDATE products SET pending_read_price = ?, pending_read_count = ?, "
            "pending_read_streak = ? WHERE id = ?",
            (_dec_str(price), count, streak, product_id),
        )
        await self._conn.commit()

    async def clear_pending_read(self, product_id: int) -> None:
        """Forget any held read — the latest scrape was plausible on its own."""
        await self._conn.execute(
            "UPDATE products SET pending_read_price = NULL, pending_read_count = 0, "
            "pending_read_streak = 0 WHERE id = ?",
            (product_id,),
        )
        await self._conn.commit()

    # ── Price history ──────────────────────────────────────────

    async def add_price_history(self, product_id: int, price: Decimal) -> None:
        await self._conn.execute(
            "INSERT INTO price_history(product_id, price) VALUES(?, ?)",
            (product_id, _dec_str(price)),
        )
        await self._conn.commit()

    async def get_price_history(
        self, product_id: int, *, limit: int = 100
    ) -> list[PriceHistoryRecord]:
        """Readings for a product, newest first."""
        cursor = await self._conn.execute(
            "SELECT id, product_id, price, checked_at FROM price_history "
            "WHERE product_id = ? ORDER BY checked_at DESC, id DESC LIMIT ?",
            (product_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            PriceHistoryRecord(
                id=r[0],
                product_id=r[1],
                price=_dec(r[2]) or Decimal("0"),
                checked_at=r[3],
            )
            for r in rows
        ]

    async def get_price_change_points(
        self, product_id: int, *, since: str
    ) -> list[PriceHistoryRecord]:
        """Return a chart window's first, last, and intervening price changes.

        Price history records every check, not every movement. Collapsing the
        unchanged runs in SQLite lets a time-bounded chart retain both ends of
        its full window without transferring an arbitrary number of samples.
        """
        cursor = await self._conn.execute(
            """
            WITH windowed AS (
                SELECT
                    id,
                    product_id,
                    price,
                    checked_at,
                    replace(replace(checked_at, 'T', ' '), 'Z', '') AS normalized_checked_at
                FROM price_history
                WHERE product_id = ?
                  AND replace(replace(checked_at, 'T', ' '), 'Z', '') >= ?
            ), numbered AS (
                SELECT
                    id,
                    product_id,
                    price,
                    checked_at,
                    normalized_checked_at,
                    LAG(price) OVER (
                        PARTITION BY product_id
                        ORDER BY normalized_checked_at ASC, id ASC
                    ) AS previous_price,
                    ROW_NUMBER() OVER (
                        PARTITION BY product_id
                        ORDER BY normalized_checked_at ASC, id ASC
                    ) AS row_number,
                    COUNT(*) OVER (PARTITION BY product_id) AS window_row_count
                FROM windowed
            )
            SELECT id, product_id, price, checked_at
            FROM numbered
            WHERE previous_price IS NULL
               OR price <> previous_price
               OR row_number = window_row_count
            ORDER BY normalized_checked_at ASC, id ASC
            """,
            (product_id, since),
        )
        rows = await cursor.fetchall()
        return [
            PriceHistoryRecord(
                id=r[0],
                product_id=r[1],
                price=_dec(r[2]) or Decimal("0"),
                checked_at=r[3],
            )
            for r in rows
        ]

    async def delete_old_price_history(self, *, days: int) -> int:
        cursor = await self._conn.execute(
            "DELETE FROM price_history WHERE checked_at < datetime('now', ? || ' days')",
            (f"-{days}",),
        )
        await self._conn.commit()
        return int(cursor.rowcount)

    # ── Scraper Health ─────────────────────────────────────────

    async def get_scraper_health(self, domain: str) -> ScraperHealth | None:
        cursor = await self._conn.execute(
            """
            SELECT domain, state, consecutive_blocks, locked_until,
                   last_block_at, last_block_reason, last_success_at, updated_at
            FROM scraper_health WHERE domain = ?
            """,
            (domain,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return ScraperHealth(
            domain=row[0],
            state=row[1],
            consecutive_blocks=row[2],
            locked_until=_parse_ts(row[3]),
            last_block_at=_parse_ts(row[4]),
            last_block_reason=row[5],
            last_success_at=_parse_ts(row[6]),
            updated_at=_parse_ts(row[7]),
        )

    async def upsert_scraper_health(self, record: ScraperHealth) -> None:
        await self._conn.execute(
            """
            INSERT INTO scraper_health
                (domain, state, consecutive_blocks, locked_until,
                 last_block_at, last_block_reason, last_success_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(domain) DO UPDATE SET
                state = excluded.state,
                consecutive_blocks = excluded.consecutive_blocks,
                locked_until = excluded.locked_until,
                last_block_at = excluded.last_block_at,
                last_block_reason = excluded.last_block_reason,
                last_success_at = excluded.last_success_at,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                record.domain,
                record.state,
                record.consecutive_blocks,
                record.locked_until.isoformat() if record.locked_until else None,
                record.last_block_at.isoformat() if record.last_block_at else None,
                record.last_block_reason,
                record.last_success_at.isoformat() if record.last_success_at else None,
            ),
        )
        await self._conn.commit()

    async def list_locked_domains(self) -> list[ScraperHealth]:
        cursor = await self._conn.execute(
            """
            SELECT domain, state, consecutive_blocks, locked_until,
                   last_block_at, last_block_reason, last_success_at, updated_at
            FROM scraper_health
            WHERE state LIKE 'LOCKED_%' OR state LIKE 'HALF_OPEN_%'
            ORDER BY locked_until IS NULL ASC, locked_until ASC
            """
        )
        rows = await cursor.fetchall()
        return [
            ScraperHealth(
                domain=r[0],
                state=r[1],
                consecutive_blocks=r[2],
                locked_until=_parse_ts(r[3]),
                last_block_at=_parse_ts(r[4]),
                last_block_reason=r[5],
                last_success_at=_parse_ts(r[6]),
                updated_at=_parse_ts(r[7]),
            )
            for r in rows
        ]

    async def list_all_scraper_health(self) -> list[ScraperHealth]:
        cursor = await self._conn.execute(
            """
            SELECT domain, state, consecutive_blocks, locked_until,
                   last_block_at, last_block_reason, last_success_at, updated_at
            FROM scraper_health
            ORDER BY domain
            """
        )
        rows = await cursor.fetchall()
        return [
            ScraperHealth(
                domain=r[0],
                state=r[1],
                consecutive_blocks=r[2],
                locked_until=_parse_ts(r[3]),
                last_block_at=_parse_ts(r[4]),
                last_block_reason=r[5],
                last_success_at=_parse_ts(r[6]),
                updated_at=_parse_ts(r[7]),
            )
            for r in rows
        ]

    # ── Test helpers (minimal user/product setup) ──────────────

    async def create_user(self, *, user_id: int) -> None:
        """Create a user if it does not exist. Test/admin helper."""
        await self.ensure_user(user_id=user_id)

    async def create_product(self, *, product_id: int, user_id: int, url: str) -> None:
        """Insert a product with explicit id. Test helper for FK setup."""
        await self._conn.execute(
            "INSERT INTO products (id, user_id, url) VALUES (?, ?, ?)",
            (product_id, user_id, url),
        )
        await self._conn.commit()

    # ── Notification prefs ─────────────────────────────────────

    async def get_notification_prefs(
        self, *, user_id: int, product_id: int | None
    ) -> NotificationPrefs | None:
        if product_id is None:
            cursor = await self._conn.execute(
                "SELECT user_id, product_id, mute, mute_until, digest_mode, "
                "digest_interval_minutes, quiet_hours_start, quiet_hours_end, "
                "throttle_per_hour, timezone, throttle_state_json, updated_at "
                "FROM notification_prefs WHERE user_id = ? AND product_id IS NULL",
                (user_id,),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT user_id, product_id, mute, mute_until, digest_mode, "
                "digest_interval_minutes, quiet_hours_start, quiet_hours_end, "
                "throttle_per_hour, timezone, throttle_state_json, updated_at "
                "FROM notification_prefs WHERE user_id = ? AND product_id = ?",
                (user_id, product_id),
            )
        row = await cursor.fetchone()
        if row is None:
            return None
        return NotificationPrefs(
            user_id=row[0],
            product_id=row[1],
            mute=bool(row[2]),
            mute_until=_parse_ts(row[3]),
            digest_mode=bool(row[4]),
            digest_interval_minutes=row[5],
            quiet_hours_start=row[6],
            quiet_hours_end=row[7],
            throttle_per_hour=row[8],
            timezone=row[9],
            throttle_state_json=row[10],
            updated_at=_parse_ts(row[11]),
        )

    async def upsert_notification_prefs(self, prefs: NotificationPrefs) -> None:
        if prefs.product_id is None:
            # The (user_id, product_id) PK never fires for NULL product_id
            # because SQLite treats NULLs as distinct; the partial unique
            # index ux_notification_prefs_global (migration 011) makes a
            # single atomic upsert possible — the previous SELECT-then-INSERT
            # emulation raced under concurrency and duplicated rows (#58).
            await self._conn.execute(
                """
                INSERT INTO notification_prefs (
                    user_id, product_id, mute, mute_until, digest_mode,
                    digest_interval_minutes, quiet_hours_start, quiet_hours_end,
                    throttle_per_hour, timezone, throttle_state_json
                )
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) WHERE product_id IS NULL DO UPDATE SET
                    mute = excluded.mute,
                    mute_until = excluded.mute_until,
                    digest_mode = excluded.digest_mode,
                    digest_interval_minutes = excluded.digest_interval_minutes,
                    quiet_hours_start = excluded.quiet_hours_start,
                    quiet_hours_end = excluded.quiet_hours_end,
                    throttle_per_hour = excluded.throttle_per_hour,
                    timezone = excluded.timezone,
                    throttle_state_json = excluded.throttle_state_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    prefs.user_id,
                    int(prefs.mute),
                    prefs.mute_until.isoformat() if prefs.mute_until else None,
                    int(prefs.digest_mode),
                    prefs.digest_interval_minutes,
                    prefs.quiet_hours_start,
                    prefs.quiet_hours_end,
                    prefs.throttle_per_hour,
                    prefs.timezone,
                    prefs.throttle_state_json,
                ),
            )
        else:
            await self._conn.execute(
                """
                INSERT INTO notification_prefs (
                    user_id, product_id, mute, mute_until, digest_mode,
                    digest_interval_minutes, quiet_hours_start, quiet_hours_end,
                    throttle_per_hour, timezone, throttle_state_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, product_id) DO UPDATE SET
                    mute = excluded.mute,
                    mute_until = excluded.mute_until,
                    digest_mode = excluded.digest_mode,
                    digest_interval_minutes = excluded.digest_interval_minutes,
                    quiet_hours_start = excluded.quiet_hours_start,
                    quiet_hours_end = excluded.quiet_hours_end,
                    throttle_per_hour = excluded.throttle_per_hour,
                    timezone = excluded.timezone,
                    throttle_state_json = excluded.throttle_state_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    prefs.user_id,
                    prefs.product_id,
                    int(prefs.mute),
                    prefs.mute_until.isoformat() if prefs.mute_until else None,
                    int(prefs.digest_mode),
                    prefs.digest_interval_minutes,
                    prefs.quiet_hours_start,
                    prefs.quiet_hours_end,
                    prefs.throttle_per_hour,
                    prefs.timezone,
                    prefs.throttle_state_json,
                ),
            )
        await self._conn.commit()

    # ── Digest queue ───────────────────────────────────────────

    async def enqueue_digest(self, *, user_id: int, product_id: int | None, payload: str) -> int:
        cursor = await self._conn.execute(
            "INSERT INTO digest_queue (user_id, product_id, alert_payload_json) VALUES (?, ?, ?)",
            (user_id, product_id, payload),
        )
        await self._conn.commit()
        rid = cursor.lastrowid
        assert rid is not None  # AUTOINCREMENT PK always returns a row id
        return int(rid)

    async def list_pending_digest(self, *, user_id: int) -> list[DigestEntry]:
        cursor = await self._conn.execute(
            "SELECT id, user_id, product_id, alert_payload_json, "
            "enqueued_at, flushed_at "
            "FROM digest_queue "
            "WHERE user_id = ? AND flushed_at IS NULL "
            "ORDER BY enqueued_at",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [
            DigestEntry(
                id=r[0],
                user_id=r[1],
                product_id=r[2],
                alert_payload_json=r[3],
                enqueued_at=_parse_ts(r[4]),
                flushed_at=_parse_ts(r[5]),
            )
            for r in rows
        ]

    async def mark_digest_flushed(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        await self._conn.execute(
            f"UPDATE digest_queue SET flushed_at = CURRENT_TIMESTAMP "  # noqa: S608
            f"WHERE id IN ({placeholders})",
            ids,
        )
        await self._conn.commit()

    async def list_users_with_pending_digest(self) -> list[tuple[int, datetime]]:
        """Return (user_id, oldest_enqueued_at) for users with pending digest entries."""
        cursor = await self._conn.execute(
            "SELECT user_id, MIN(enqueued_at) FROM digest_queue "
            "WHERE flushed_at IS NULL GROUP BY user_id"
        )
        rows = await cursor.fetchall()
        return [(r[0], dt) for r in rows if (dt := _parse_ts(r[1])) is not None]

    # ── Backward-compat aliases for legacy handler API (Plan 1 F1 drift) ──
    #
    # The pre-refactor monolith spoke to a dict-row repository with a different
    # method naming. Handlers under ``bot/handlers/`` were ported "as-is" and
    # still use the legacy names. v0.1.4 adds these thin wrappers so the
    # handlers keep working while the underlying repository stays typed.
    # The contract test ``tests/integration/test_repository_handler_contract``
    # enforces that every ``db.<method>(...)`` call in handlers resolves here.

    async def get_product_by_url_for_user(self, url: str, user_id: int) -> ProductRecord | None:
        """Look up a product by ``(url, user_id)`` — used by ``/add`` dedup."""
        cursor = await self._conn.execute(
            f"SELECT {_PRODUCT_COLS} FROM products WHERE url = ? AND user_id = ?",
            (url, user_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_product(tuple(row))

    async def get_product_for_user(self, product_id: int, user_id: int) -> ProductRecord | None:
        """Like :meth:`get_product` but scoped to the caller (admin uses get_product)."""
        cursor = await self._conn.execute(
            f"SELECT {_PRODUCT_COLS} FROM products WHERE id = ? AND user_id = ?",
            (product_id, user_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_product(tuple(row))

    async def is_user_admin(self, user_id: int) -> bool:
        cursor = await self._conn.execute(
            "SELECT is_admin FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return bool(row and row[0])

    async def add_user(self, user_id: int, *, is_admin: bool = False) -> None:
        """Alias of :meth:`ensure_user`."""
        await self.ensure_user(user_id=user_id, is_admin=is_admin)

    async def get_all_users(self) -> list[UserRecord]:
        """Alias of :meth:`list_users`."""
        return await self.list_users()

    async def get_active_products(self, user_id: int) -> list[ProductRecord]:
        """Alias of ``list_products_for_user(user_id, only_active=True)``."""
        return await self.list_products_for_user(user_id=user_id, only_active=True)

    async def get_all_products(self, user_id: int) -> list[ProductRecord]:
        """Alias of :meth:`list_products_for_user`."""
        return await self.list_products_for_user(user_id=user_id)

    async def deactivate_product(self, product_id: int) -> None:
        """Alias of :meth:`pause_product`."""
        await self.pause_product(product_id)

    async def cleanup_old_history(self, *, retention_days: int) -> int:
        """Alias of :meth:`delete_old_price_history` (keyword-renamed)."""
        return await self.delete_old_price_history(days=retention_days)

    async def set_product_interval(self, product_id: int, minutes: int | None) -> None:
        """Alias of :meth:`set_check_interval`."""
        await self.set_check_interval(product_id, minutes)

    async def reset_initial_price(self, product_id: int) -> bool:
        """Reset ``initial_price`` to the current price. Returns True if a row was updated."""
        cursor = await self._conn.execute(
            "UPDATE products SET initial_price = current_price, "
            "updated_at = datetime('now') "
            "WHERE id = ? AND current_price IS NOT NULL",
            (product_id,),
        )
        await self._conn.commit()
        return int(cursor.rowcount) > 0

    async def set_product_preferences(
        self,
        product_id: int,
        *,
        condition: str | None = None,
        seller: str | None = None,
    ) -> None:
        """Update preferred_condition / preferred_seller for a product."""
        await self._conn.execute(
            "UPDATE products SET preferred_condition = ?, preferred_seller = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (condition, seller, product_id),
        )
        await self._conn.commit()

    async def get_stats(self, user_id: int | None = None) -> dict[str, int]:
        """Return ``{active_products, total_products, total_checks}``.

        With ``user_id`` scopes counts to that user; without it returns globals.
        Handlers consume the result via ``stats["active_products"]`` etc.
        """
        if user_id is None:
            cur = await self._conn.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0), "
                "COUNT(*) "
                "FROM products"
            )
            row = await cur.fetchone()
            active_count = int(row[0]) if row else 0
            total_count = int(row[1]) if row else 0
            cur2 = await self._conn.execute("SELECT COUNT(*) FROM price_history")
            row2 = await cur2.fetchone()
            total_checks = int(row2[0]) if row2 else 0
        else:
            cur = await self._conn.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0), "
                "COUNT(*) "
                "FROM products WHERE user_id = ?",
                (user_id,),
            )
            row = await cur.fetchone()
            active_count = int(row[0]) if row else 0
            total_count = int(row[1]) if row else 0
            cur2 = await self._conn.execute(
                "SELECT COUNT(*) FROM price_history ph "
                "JOIN products p ON p.id = ph.product_id "
                "WHERE p.user_id = ?",
                (user_id,),
            )
            row2 = await cur2.fetchone()
            total_checks = int(row2[0]) if row2 else 0
        return {
            "active_products": active_count,
            "total_products": total_count,
            "total_checks": total_checks,
        }
