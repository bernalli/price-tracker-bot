"""Price-history operations for the SQLite repository."""

from __future__ import annotations

from decimal import Decimal

from price_tracker.db.models import PriceHistoryRecord
from price_tracker.db.repository_common import _dec, _dec_str, _RepositoryBase


class HistoryRepositoryMixin(_RepositoryBase):
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
