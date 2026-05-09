"""SQLite repository — typed CRUD over schema applied by migrator."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from price_tracker.db.models import PriceHistoryRecord, ProductRecord, UserRecord

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


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
            "UPDATE products SET pending_alert_price = NULL, "
            "pending_alert_at = NULL WHERE id = ?",
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
