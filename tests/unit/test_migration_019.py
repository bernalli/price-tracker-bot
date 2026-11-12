"""Status persistence upgrade preserves a populated production-shaped schema 18."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from price_tracker.notifier.digest import DigestService


@pytest.mark.asyncio
async def test_upgrade_preserves_products_history_and_starts_with_no_fabricated_notices():
    migrations = Path("src/price_tracker/db/migrations")
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await apply_migrations(conn, migrations, max_version=18)
        await conn.execute("INSERT INTO users(user_id) VALUES (1)")
        await conn.execute(
            "INSERT INTO products(user_id,url,is_available,current_price,is_active,"
            "suspension_kind,suspension_reason) VALUES "
            "(1,'https://example.com/stock',0,'100',1,NULL,NULL),"
            "(1,'https://example.com/gone',1,'20',0,'automatic','listing_gone')"
        )
        await conn.execute("INSERT INTO price_history(product_id,price) VALUES (1,'100')")
        await conn.commit()
        assert await apply_migrations(conn, migrations) == 19
        repo = Repository(conn)
        products = await repo.list_products_for_user(user_id=1)
        assert [(p.is_active, p.is_available, str(p.current_price)) for p in products] == [
            (True, False, "100"),
            (False, True, "20"),
        ]
        assert await repo.list_pending_status_notices() == []
        assert [str(h.price) for h in await repo.get_price_history(1)] == ["100"]
        assert await apply_migrations(conn, migrations) == 19
        integrity = await (await conn.execute("PRAGMA integrity_check")).fetchone()
        assert integrity is not None
        assert tuple(integrity) == ("ok",)


@pytest.mark.asyncio
async def test_legacy_duplicate_statuses_upgrade_and_filter_live_group_members():
    migrations = Path("src/price_tracker/db/migrations")
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await apply_migrations(conn, migrations, max_version=18)
        await conn.execute("INSERT INTO users(user_id) VALUES (1)")
        await conn.execute(
            "INSERT INTO products(user_id,url,name,is_active,suspension_kind,suspension_reason) "
            "VALUES (1,'https://example.com/1','Live',0,'automatic','parse_error'),"
            "(1,'https://example.com/2','Resumed',1,NULL,NULL)"
        )
        payload = json.dumps(
            {
                "kind": "operational",
                "event": "suspended",
                "event_id": "old-duplicate",
                "domain": "example.com",
                "count": 2,
                "reason": "parse_error",
                "products": [{"id": 1, "name": "Live"}, {"id": 2, "name": "Resumed"}],
            }
        )
        for _ in range(2):
            await conn.execute(
                "INSERT INTO digest_queue(user_id,alert_payload_json) VALUES (1,?)", (payload,)
            )
        await conn.commit()
        assert await apply_migrations(conn, migrations) == 19
        repo = Repository(conn)
        bot = AsyncMock()
        await DigestService(repo=repo, bot=bot, lang="en").flush_user(user_id=1)
        text = bot.send_message.await_args.kwargs["text"]
        assert text.count("#1:") == 1
        assert "#2:" not in text
        assert "Resumed" not in text
        assert "price not readable" in text
        assert await repo.list_pending_digest(user_id=1) == []


@pytest.mark.asyncio
async def test_legacy_status_without_identity_is_marked_historical():
    async with aiosqlite.connect(":memory:") as conn:
        await apply_migrations(conn, Path("src/price_tracker/db/migrations"))
        repo = Repository(conn)
        await repo.ensure_user(user_id=1)
        await repo.enqueue_digest(
            user_id=1,
            product_id=None,
            payload='{"kind":"operational","event":"suspended","domain":"example.com"}',
        )
        bot = AsyncMock()
        await DigestService(repo=repo, bot=bot, lang="en").flush_user(user_id=1)
        text = bot.send_message.await_args.kwargs["text"]
        assert "Previous notice" in text
        assert "tracking suspended" not in text
