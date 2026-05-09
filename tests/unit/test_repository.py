"""Tests for the SQLite repository layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import pytest
import pytest_asyncio

from price_tracker.db.migrator import apply_migrations
from price_tracker.db.models import ScraperHealth
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    try:
        yield Repository(conn)
    finally:
        await conn.close()


async def test_add_product_returns_id(repo: Repository):
    pid = await repo.add_product(
        user_id=1,
        url="https://example.com/p/1",
        name="Widget",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    assert pid > 0


async def test_get_product_round_trip(repo: Repository):
    pid = await repo.add_product(
        user_id=1,
        url="https://example.com/p/1",
        name="Widget",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    p = await repo.get_product(pid)
    assert p is not None
    assert p.id == pid
    assert p.name == "Widget"
    assert p.initial_price == Decimal("100")
    assert p.currency == "EUR"


async def test_list_products_for_user(repo: Repository):
    await repo.add_product(
        user_id=1,
        url="https://x/1",
        name="A",
        domain="x",
        initial_price=Decimal("10"),
        currency="EUR",
    )
    await repo.add_product(
        user_id=1,
        url="https://x/2",
        name="B",
        domain="x",
        initial_price=Decimal("20"),
        currency="EUR",
    )
    await repo.add_product(
        user_id=2,
        url="https://x/3",
        name="C",
        domain="x",
        initial_price=Decimal("30"),
        currency="EUR",
    )
    items = await repo.list_products_for_user(user_id=1)
    names = [i.name for i in items]
    assert "A" in names
    assert "B" in names
    assert "C" not in names


async def test_set_config_and_get_config(repo: Repository):
    assert await repo.get_config("foo") is None
    await repo.set_config("foo", "bar")
    assert await repo.get_config("foo") == "bar"
    await repo.set_config("foo", "baz")
    assert await repo.get_config("foo") == "baz"


async def test_user_admin_flow(repo: Repository):
    await repo.ensure_user(user_id=42, is_admin=True)
    assert await repo.is_user_allowed(42) is True
    user = await repo.get_user(42)
    assert user is not None
    assert user.is_admin is True


async def test_increment_errors_and_reset(repo: Repository):
    pid = await repo.add_product(
        user_id=1,
        url="https://x/1",
        name="A",
        domain="x",
        initial_price=Decimal("10"),
        currency="EUR",
    )
    await repo.increment_errors(pid)
    await repo.increment_errors(pid)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.consecutive_errors == 2
    await repo.reset_errors(pid)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.consecutive_errors == 0


async def test_add_price_history_and_query(repo: Repository):
    pid = await repo.add_product(
        user_id=1,
        url="https://x/1",
        name="A",
        domain="x",
        initial_price=Decimal("10"),
        currency="EUR",
    )
    await repo.add_price_history(pid, Decimal("9"))
    await repo.add_price_history(pid, Decimal("8"))
    history = await repo.get_price_history(pid, limit=10)
    assert len(history) == 2
    # Ordered DESC by checked_at
    assert history[0].price == Decimal("8")
    assert history[1].price == Decimal("9")


async def test_get_user_returns_none_when_missing(repo: Repository):
    """get_user on unknown user_id → None (covers line 115)."""
    assert await repo.get_user(99999) is None


async def test_update_user_info_sets_display_name(repo: Repository):
    """update_user_info merges display_name and username via COALESCE."""
    await repo.ensure_user(user_id=1)
    await repo.update_user_info(user_id=1, display_name="Alice", username="alice42")
    user = await repo.get_user(1)
    assert user is not None
    assert user.display_name == "Alice"
    assert user.username == "alice42"


async def test_set_admin_toggle(repo: Repository):
    """set_admin updates is_admin both ways."""
    await repo.ensure_user(user_id=1)
    await repo.set_admin(1, True)
    user = await repo.get_user(1)
    assert user is not None
    assert user.is_admin is True
    await repo.set_admin(1, False)
    user = await repo.get_user(1)
    assert user is not None
    assert user.is_admin is False


async def test_remove_user_marks_inactive(repo: Repository):
    """remove_user is a soft-delete: sets is_active=0."""
    await repo.ensure_user(user_id=1)
    await repo.remove_user(1)
    user = await repo.get_user(1)
    assert user is not None
    assert user.is_active is False


async def test_list_users_returns_all(repo: Repository):
    await repo.ensure_user(user_id=1)
    await repo.ensure_user(user_id=2)
    users = await repo.list_users()
    user_ids = {u.user_id for u in users}
    assert 1 in user_ids
    assert 2 in user_ids


async def test_list_active_users_filters_inactive(repo: Repository):
    await repo.ensure_user(user_id=1)
    await repo.ensure_user(user_id=2)
    await repo.remove_user(2)
    active = await repo.list_active_users()
    user_ids = {u.user_id for u in active}
    assert 1 in user_ids
    assert 2 not in user_ids


async def test_ensure_admin_users_bulk(repo: Repository):
    """ensure_admin_users calls ensure_user for each id with is_admin=True."""
    await repo.ensure_admin_users((10, 20, 30))
    for uid in (10, 20, 30):
        user = await repo.get_user(uid)
        assert user is not None
        assert user.is_admin is True


async def test_delete_product_returns_false_when_not_found(repo: Repository):
    """delete_product returns False if no row matches user_id."""
    deleted = await repo.delete_product(99999, user_id=1)
    assert deleted is False


async def test_delete_product_returns_true_when_deleted(repo: Repository):
    pid = await repo.add_product(
        user_id=1,
        url="https://x/del",
        name="Del",
        domain="x",
        initial_price=Decimal("5"),
        currency="EUR",
    )
    deleted = await repo.delete_product(pid, user_id=1)
    assert deleted is True
    assert await repo.get_product(pid) is None


async def test_update_price_tracks_lowest_and_highest(repo: Repository):
    pid = await repo.add_product(
        user_id=1,
        url="https://x/up",
        name="Up",
        domain="x",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    await repo.update_price(pid, Decimal("80"))
    p = await repo.get_product(pid)
    assert p is not None
    assert p.current_price == Decimal("80")
    assert p.lowest_price == Decimal("80")
    assert p.highest_price == Decimal("100")
    await repo.update_price(pid, Decimal("120"))
    p = await repo.get_product(pid)
    assert p is not None
    assert p.highest_price == Decimal("120")
    assert p.lowest_price == Decimal("80")


async def test_set_threshold_and_target(repo: Repository):
    pid = await repo.add_product(
        user_id=1,
        url="https://x/th",
        name="Th",
        domain="x",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    await repo.set_threshold(pid, "fixed", Decimal("90"))
    await repo.set_target_price(pid, Decimal("85"))
    p = await repo.get_product(pid)
    assert p is not None
    assert p.threshold_type == "fixed"
    assert p.threshold_value == Decimal("90")
    assert p.target_price == Decimal("85")
    # Clear target
    await repo.set_target_price(pid, None)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.target_price is None


async def test_set_check_interval(repo: Repository):
    pid = await repo.add_product(
        user_id=1,
        url="https://x/iv",
        name="Iv",
        domain="x",
        initial_price=Decimal("1"),
        currency="EUR",
    )
    await repo.set_check_interval(pid, 60)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.check_interval_minutes == 60


async def test_pause_and_reactivate_product(repo: Repository):
    pid = await repo.add_product(
        user_id=1,
        url="https://x/pa",
        name="Pa",
        domain="x",
        initial_price=Decimal("1"),
        currency="EUR",
    )
    await repo.pause_product(pid)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.is_active is False
    await repo.reactivate_product(pid)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.is_active is True
    assert p.consecutive_errors == 0


async def test_pending_alert_round_trip(repo: Repository):
    pid = await repo.add_product(
        user_id=1,
        url="https://x/pe",
        name="Pe",
        domain="x",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    await repo.mark_pending_alert(pid, Decimal("85"))
    p = await repo.get_product(pid)
    assert p is not None
    assert p.pending_alert_price == Decimal("85")
    await repo.clear_pending_alert(pid)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.pending_alert_price is None


async def test_delete_old_price_history(repo: Repository):
    pid = await repo.add_product(
        user_id=1,
        url="https://x/hist",
        name="Hist",
        domain="x",
        initial_price=Decimal("1"),
        currency="EUR",
    )
    await repo.add_price_history(pid, Decimal("9"))
    # Nothing is older than 365 days yet → 0
    deleted = await repo.delete_old_price_history(days=365)
    assert deleted == 0


async def test_dec_returns_none_on_invalid_string(repo: Repository):
    """_dec helper returns None on un-parsable values (line 23-24)."""
    from price_tracker.db.repository import _dec

    assert _dec(None) is None
    assert _dec("not-a-decimal") is None
    assert _dec("1.5") == Decimal("1.5")


class TestScraperHealthRepository:
    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing_domain(self, repo: Repository):
        result = await repo.get_scraper_health("nonexistent.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_upsert_then_get_roundtrip(self, repo: Repository):
        now = datetime.now(UTC).replace(microsecond=0)
        record = ScraperHealth(
            domain="xteink.com",
            state="LOCKED_T1",
            consecutive_blocks=3,
            locked_until=now + timedelta(hours=1),
            last_block_at=now,
            last_block_reason="HTTP 429",
            last_success_at=None,
        )
        await repo.upsert_scraper_health(record)
        loaded = await repo.get_scraper_health("xteink.com")
        assert loaded is not None
        assert loaded.domain == "xteink.com"
        assert loaded.state == "LOCKED_T1"
        assert loaded.consecutive_blocks == 3
        assert loaded.last_block_reason == "HTTP 429"
        assert loaded.locked_until == record.locked_until
        assert loaded.last_block_at == record.last_block_at

    @pytest.mark.asyncio
    async def test_upsert_overwrites(self, repo: Repository):
        first = ScraperHealth(domain="x.com", state="CLOSED", consecutive_blocks=0)
        await repo.upsert_scraper_health(first)
        second = ScraperHealth(domain="x.com", state="LOCKED_T2", consecutive_blocks=6)
        await repo.upsert_scraper_health(second)
        loaded = await repo.get_scraper_health("x.com")
        assert loaded.state == "LOCKED_T2"
        assert loaded.consecutive_blocks == 6

    @pytest.mark.asyncio
    async def test_list_locked_filters_correctly(self, repo: Repository):
        now = datetime.now(UTC)
        future = now + timedelta(hours=1)
        await repo.upsert_scraper_health(ScraperHealth(domain="closed.com", state="CLOSED"))
        await repo.upsert_scraper_health(
            ScraperHealth(domain="locked.com", state="LOCKED_T1", locked_until=future)
        )
        locked = await repo.list_locked_domains()
        assert {h.domain for h in locked} == {"locked.com"}

    @pytest.mark.asyncio
    async def test_list_locked_ordering_nulls_last(self, repo: Repository):
        """HALF_OPEN rows (locked_until IS NULL) must come after LOCKED rows."""
        now = datetime.now(UTC)
        future = now + timedelta(hours=1)
        await repo.upsert_scraper_health(ScraperHealth(domain="half.com", state="HALF_OPEN_T1"))
        await repo.upsert_scraper_health(
            ScraperHealth(domain="locked.com", state="LOCKED_T2", locked_until=future)
        )
        results = await repo.list_locked_domains()
        domains = [h.domain for h in results]
        assert domains.index("locked.com") < domains.index("half.com")

    @pytest.mark.asyncio
    async def test_list_all_returns_every_record(self, repo: Repository):
        await repo.upsert_scraper_health(ScraperHealth(domain="a.com", state="CLOSED"))
        await repo.upsert_scraper_health(ScraperHealth(domain="b.com", state="LOCKED_T1"))
        all_records = await repo.list_all_scraper_health()
        domains = {h.domain for h in all_records}
        assert domains == {"a.com", "b.com"}


class TestNotificationPrefsRepository:
    @pytest.mark.asyncio
    async def test_get_prefs_none_for_missing(self, repo: Repository):
        result = await repo.get_notification_prefs(user_id=1, product_id=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_upsert_global_then_get(self, repo: Repository):
        from price_tracker.db.models import NotificationPrefs

        await repo.create_user(user_id=1)
        prefs = NotificationPrefs(
            user_id=1, product_id=None, digest_mode=True, digest_interval_minutes=30
        )
        await repo.upsert_notification_prefs(prefs)
        loaded = await repo.get_notification_prefs(user_id=1, product_id=None)
        assert loaded is not None
        assert loaded.digest_mode is True
        assert loaded.digest_interval_minutes == 30

    @pytest.mark.asyncio
    async def test_upsert_per_product_does_not_clash_with_global(self, repo: Repository):
        from price_tracker.db.models import NotificationPrefs

        await repo.create_user(user_id=1)
        await repo.create_product(product_id=10, user_id=1, url="https://x.com/1")
        global_prefs = NotificationPrefs(user_id=1, product_id=None, digest_mode=True)
        per_product = NotificationPrefs(user_id=1, product_id=10, digest_mode=False)
        await repo.upsert_notification_prefs(global_prefs)
        await repo.upsert_notification_prefs(per_product)
        loaded_global = await repo.get_notification_prefs(user_id=1, product_id=None)
        loaded_per = await repo.get_notification_prefs(user_id=1, product_id=10)
        assert loaded_global is not None
        assert loaded_per is not None
        assert loaded_global.digest_mode is True
        assert loaded_per.digest_mode is False

    @pytest.mark.asyncio
    async def test_upsert_global_updates_existing(self, repo: Repository):
        """Second upsert with product_id=None must hit the UPDATE branch."""
        from price_tracker.db.models import NotificationPrefs

        await repo.create_user(user_id=1)
        first = NotificationPrefs(
            user_id=1, product_id=None, digest_mode=False, digest_interval_minutes=60
        )
        await repo.upsert_notification_prefs(first)
        second = NotificationPrefs(
            user_id=1, product_id=None, digest_mode=True, digest_interval_minutes=15
        )
        await repo.upsert_notification_prefs(second)
        loaded = await repo.get_notification_prefs(user_id=1, product_id=None)
        assert loaded is not None
        assert loaded.digest_mode is True
        assert loaded.digest_interval_minutes == 15


class TestDigestQueueRepository:
    @pytest.mark.asyncio
    async def test_enqueue_and_list_pending(self, repo: Repository):
        await repo.create_user(user_id=1)
        await repo.create_product(product_id=10, user_id=1, url="https://x.com/1")
        eid = await repo.enqueue_digest(user_id=1, product_id=10, payload='{"k":"v"}')
        assert eid is not None
        pending = await repo.list_pending_digest(user_id=1)
        assert len(pending) == 1
        assert pending[0].user_id == 1
        assert pending[0].product_id == 10

    @pytest.mark.asyncio
    async def test_mark_flushed_excludes_from_pending(self, repo: Repository):
        await repo.create_user(user_id=1)
        await repo.create_product(product_id=10, user_id=1, url="https://x.com/1")
        eid = await repo.enqueue_digest(user_id=1, product_id=10, payload="{}")
        await repo.mark_digest_flushed([eid])
        pending = await repo.list_pending_digest(user_id=1)
        assert pending == []

    @pytest.mark.asyncio
    async def test_mark_flushed_empty_list_is_noop(self, repo: Repository):
        """Empty list short-circuits without any DB query."""
        await repo.mark_digest_flushed([])
        # No exception raised, no rows touched.
        pending = await repo.list_pending_digest(user_id=1)
        assert pending == []
