"""Configuration and scraper-health operations for the SQLite repository."""

from __future__ import annotations

from price_tracker.db.models import ScraperHealth
from price_tracker.db.repository_common import _parse_ts, _RepositoryBase


class OpsRepositoryMixin(_RepositoryBase):
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
