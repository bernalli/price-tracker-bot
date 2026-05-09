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
    "pending_alert_price, pending_alert_at, preferred_condition, preferred_seller"
)


def _row_to_product(row: tuple[Any, ...]) -> ProductRecord:
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
        threshold_value=_dec(row[11]) or Decimal("10"),
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
        await self._conn.execute("UPDATE products SET is_active = 0 WHERE id = ?", (product_id,))
        await self._conn.commit()

    async def reactivate_product(self, product_id: int) -> None:
        await self._conn.execute(
            "UPDATE products SET is_active = 1, consecutive_errors = 0 WHERE id = ?",
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
            "UPDATE products SET consecutive_errors = 0 WHERE id = ?",
            (product_id,),
        )
        await self._conn.commit()

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
            # NULL-product upsert needs partial-uniqueness emulation:
            # SQLite treats NULLs as distinct in PK, so ON CONFLICT does not fire.
            existing = await self._conn.execute(
                "SELECT 1 FROM notification_prefs WHERE user_id = ? AND product_id IS NULL",
                (prefs.user_id,),
            )
            if await existing.fetchone() is None:
                await self._conn.execute(
                    """
                    INSERT INTO notification_prefs (
                        user_id, product_id, mute, mute_until, digest_mode,
                        digest_interval_minutes, quiet_hours_start, quiet_hours_end,
                        throttle_per_hour, timezone, throttle_state_json
                    )
                    VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    UPDATE notification_prefs SET
                        mute = ?, mute_until = ?, digest_mode = ?,
                        digest_interval_minutes = ?, quiet_hours_start = ?,
                        quiet_hours_end = ?, throttle_per_hour = ?, timezone = ?,
                        throttle_state_json = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND product_id IS NULL
                    """,
                    (
                        int(prefs.mute),
                        prefs.mute_until.isoformat() if prefs.mute_until else None,
                        int(prefs.digest_mode),
                        prefs.digest_interval_minutes,
                        prefs.quiet_hours_start,
                        prefs.quiet_hours_end,
                        prefs.throttle_per_hour,
                        prefs.timezone,
                        prefs.throttle_state_json,
                        prefs.user_id,
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

    async def enqueue_digest(self, *, user_id: int, product_id: int, payload: str) -> int:
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
