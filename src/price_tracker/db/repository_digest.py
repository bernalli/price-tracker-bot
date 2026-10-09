"""Digest-queue operations for the SQLite repository."""

from __future__ import annotations

from typing import TYPE_CHECKING

from price_tracker.db.models import DigestEntry
from price_tracker.db.repository_common import _parse_ts, _RepositoryBase

if TYPE_CHECKING:
    from datetime import datetime


class DigestRepositoryMixin(_RepositoryBase):
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
