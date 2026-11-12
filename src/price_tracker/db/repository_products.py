"""Product persistence operations for the SQLite repository."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from price_tracker.db.models import ProductErrorRow, ProductRecord
from price_tracker.db.repository_common import (
    _PRODUCT_COLS,
    _dec_str,
    _RepositoryBase,
    _row_to_product,
)


class ProductRepositoryMixin(_RepositoryBase):
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
        image_url: str | None = None,
    ) -> int:
        cursor = await self._conn.execute(
            "INSERT INTO products(user_id, url, name, domain, initial_price, "
            "current_price, lowest_price, highest_price, currency, "
            "threshold_type, threshold_value, image_url) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                image_url,
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
            "UPDATE products SET current_price = ?, last_check_outcome = NULL, "
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

    async def update_price_and_image(
        self, product_id: int, price: Decimal, image_url: str | None
    ) -> None:
        """Store an accepted price and the image URL of the same reading, in one statement."""
        await self._conn.execute(
            "UPDATE products SET current_price = ?, image_url = ?, last_check_outcome = NULL, "
            "lowest_price = CASE WHEN lowest_price IS NULL OR "
            "CAST(? AS REAL) < CAST(lowest_price AS REAL) THEN ? ELSE lowest_price END, "
            "highest_price = CASE WHEN highest_price IS NULL OR "
            "CAST(? AS REAL) > CAST(highest_price AS REAL) THEN ? ELSE highest_price END, "
            "last_checked_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
            (
                _dec_str(price),
                image_url,
                _dec_str(price),
                _dec_str(price),
                _dec_str(price),
                _dec_str(price),
                product_id,
            ),
        )
        await self._conn.commit()

    async def list_image_urls(self) -> set[str]:
        """Every distinct non-NULL product image URL, for every user."""
        cursor = await self._conn.execute(
            "SELECT DISTINCT image_url FROM products WHERE image_url IS NOT NULL"
        )
        return {row[0] for row in await cursor.fetchall()}

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
            "suspension_reason = NULL, pending_notice_kind = NULL, sold_out_streak = 0, "
            "notice_version = notice_version + 1, "
            "updated_at = datetime('now') WHERE id = ?",
            (product_id,),
        )
        await self._conn.commit()

    async def reactivate_product(self, product_id: int) -> None:
        await self._conn.execute(
            "UPDATE products SET is_active = 1, consecutive_errors = 0, gone_streak = 0, "
            "suspension_kind = NULL, suspension_reason = NULL, "
            "pending_notice_kind = NULL, sold_out_streak = 0, "
            "notice_version = notice_version + 1 WHERE id = ?",
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
            "last_error = ?, last_error_at = datetime('now'), last_check_outcome = ? WHERE id = ?",
            (reason, text[:300], reason, product_id),
        )
        await self._conn.commit()
        return await self.get_product(product_id)

    async def record_block(self, product_id: int, *, detail: str | None = None) -> None:
        """Note that the store refused this check, without counting a product failure.

        A refusal says nothing about the product: ``consecutive_errors`` is left
        as it is, so the store-level pause is the only consequence. It does end a
        run of missing-page observations, which must be consecutive.
        """
        text = f"block: {detail}" if detail else "block"
        await self._conn.execute(
            "UPDATE products SET gone_streak = 0, last_error = ?, "
            "last_error_at = datetime('now'), last_check_outcome = 'block' WHERE id = ?",
            (text[:300], product_id),
        )
        await self._conn.commit()

    async def suspend_product(
        self, product_id: int, *, reason: str, queue_notice: bool = False
    ) -> bool:
        cursor = await self._conn.execute(
            "UPDATE products SET is_active = 0, suspension_kind = 'automatic', "
            "suspension_reason = ?, updated_at = datetime('now'), "
            "pending_notice_kind = CASE WHEN ? THEN 'suspended' ELSE NULL END, "
            "notice_version = notice_version + 1, notice_attempted_at = NULL "
            "WHERE id = ? AND is_active = 1",
            (reason, queue_notice, product_id),
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
        """Products of a user that carry read errors or sit behind a refusing store.

        Counted errors list active and paused products alike. A store refusal
        carries no counter: it lists the active products the store keeps unread.
        """
        cursor = await self._conn.execute(
            "SELECT id, name, url, domain, consecutive_errors, last_error, last_error_at, "
            "last_check_outcome, current_price, currency FROM products "
            "WHERE user_id = ? AND (consecutive_errors > 0 "
            "OR (is_active = 1 AND last_check_outcome IN ('block', 'quarantined', "
            "'probe_skipped'))) ORDER BY consecutive_errors DESC, id ASC",
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
                last_check_outcome=r[7],
                current_price=Decimal(r[8]) if r[8] is not None else None,
                currency=r[9],
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

    async def record_availability(self, product_id: int, *, available: bool) -> None:
        """Confirm stock observations and queue only actual transitions in one write.

        A missing/failed observation does not call this method. Two sold-out
        observations establish unavailability, including across a restart; an
        accepted available reading resets the evidence and establishes restock.
        """
        await self._conn.execute(
            "UPDATE products SET "
            "last_check_outcome = CASE WHEN ? THEN last_check_outcome ELSE 'out_of_stock' END, "
            "sold_out_streak = CASE WHEN ? THEN 0 ELSE MIN(sold_out_streak + 1, 2) END, "
            "is_available = CASE WHEN ? THEN 1 WHEN sold_out_streak >= 1 THEN 0 "
            "ELSE is_available END, "
            "pending_notice_kind = CASE "
            "WHEN ? AND is_available = 0 THEN 'restocked' "
            "WHEN NOT ? AND sold_out_streak >= 1 AND is_available = 1 THEN 'unavailable' "
            "ELSE pending_notice_kind END, "
            "notice_version = notice_version + CASE WHEN "
            "(? AND is_available = 0) OR (NOT ? AND sold_out_streak >= 1 AND is_available = 1) "
            "THEN 1 ELSE 0 END, "
            "notice_attempted_at = CASE WHEN "
            "(? AND is_available = 0) OR (NOT ? AND sold_out_streak >= 1 AND is_available = 1) "
            "THEN NULL ELSE notice_attempted_at END "
            "WHERE id = ? AND is_active = 1",
            (*([available] * 9), product_id),
        )
        await self._conn.commit()

    async def record_check_outcome(self, product_id: int, *, reason: str) -> None:
        """Persist a skipped or held observation without changing the accepted price time."""
        await self._conn.execute(
            "UPDATE products SET last_check_outcome = ? WHERE id = ?",
            (reason, product_id),
        )
        await self._conn.commit()

    async def list_pending_status_notices(self) -> list[ProductRecord]:
        """Latest undelivered status per product, including automatic suspensions."""
        cursor = await self._conn.execute(
            f"SELECT {_PRODUCT_COLS} FROM products WHERE pending_notice_kind IS NOT NULL "
            "AND user_id IN (SELECT user_id FROM users WHERE is_active = 1) ORDER BY id"
        )
        return [_row_to_product(tuple(row)) for row in await cursor.fetchall()]

    async def mark_status_notice_attempt(
        self, product_id: int, version: int, *, attempted_at: str
    ) -> bool:
        """Claim only a still-pending, current transition for an active recipient.

        A false result means the caller's snapshot was superseded or withdrawn;
        it must not send or acknowledge that event.
        """
        cursor = await self._conn.execute(
            "UPDATE products SET notice_attempted_at = ? WHERE id = ? AND notice_version = ? "
            "AND user_id IN (SELECT user_id FROM users WHERE is_active = 1) AND ("
            "(pending_notice_kind = 'suspended' AND is_active = 0 "
            "AND suspension_kind = 'automatic') OR "
            "(pending_notice_kind = 'unavailable' AND is_active = 1 AND is_available = 0) OR "
            "(pending_notice_kind = 'restocked' AND is_active = 1 AND is_available = 1))",
            (attempted_at, product_id, version),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def acknowledge_status_notice(self, product_id: int, version: int) -> None:
        """A delivery may acknowledge only the transition it actually sent."""
        await self._conn.execute(
            "UPDATE products SET pending_notice_kind = NULL, notice_attempted_at = NULL "
            "WHERE id = ? AND notice_version = ?",
            (product_id, version),
        )
        await self._conn.commit()

    async def mark_checked(self, product_id: int) -> None:
        """Stamp ``last_checked_at`` for a check that completed without writing a price."""
        await self._conn.execute(
            "UPDATE products SET last_checked_at = datetime('now') WHERE id = ?",
            (product_id,),
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

    async def create_product(self, *, product_id: int, user_id: int, url: str) -> None:
        """Insert a product with explicit id. Test helper for FK setup."""
        await self._conn.execute(
            "INSERT INTO products (id, user_id, url) VALUES (?, ?, ?)",
            (product_id, user_id, url),
        )
        await self._conn.commit()

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

    async def get_active_products(self, user_id: int) -> list[ProductRecord]:
        """Alias of ``list_products_for_user(user_id, only_active=True)``."""
        return await self.list_products_for_user(user_id=user_id, only_active=True)

    async def get_all_products(self, user_id: int) -> list[ProductRecord]:
        """Alias of :meth:`list_products_for_user`."""
        return await self.list_products_for_user(user_id=user_id)

    async def deactivate_product(self, product_id: int) -> None:
        """Alias of :meth:`pause_product`."""
        await self.pause_product(product_id)

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
