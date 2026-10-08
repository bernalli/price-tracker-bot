"""``update_prefs``: one way to change notification preferences, keeping every other field.

A product row is born with the digest and time zone of the global row, so it does not
hide them when the preferences are resolved for that product.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest
import pytest_asyncio

import price_tracker
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.models import NotificationPrefs
from price_tracker.db.repository import Repository
from price_tracker.notifier.preferences import PreferencesManager, update_prefs
from tests.unit.test_nav_dispatch import press, run_command

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path(price_tracker.__file__).resolve().parent / "db" / "migrations"
USER = 10

# Every changeable field with a value different from its default.
NON_DEFAULT: dict[str, Any] = {
    "mute": True,
    "mute_until": datetime(2030, 1, 1, tzinfo=UTC),
    "digest_mode": True,
    "digest_interval_minutes": 30,
    "quiet_hours_start": "23:00",
    "quiet_hours_end": "07:00",
    "throttle_per_hour": 5,
    "timezone": "Asia/Tokyo",
    "throttle_state_json": '{"ts": []}',
}
# A second non-default value per field, to change one at a time.
OTHER_VALUE: dict[str, Any] = {
    "mute": False,
    "mute_until": datetime(2031, 6, 1, tzinfo=UTC),
    "digest_mode": False,
    "digest_interval_minutes": 45,
    "quiet_hours_start": "21:30",
    "quiet_hours_end": "06:30",
    "throttle_per_hour": 9,
    "timezone": "America/New_York",
    "throttle_state_json": '{"ts": [1.0]}',
}


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repository = Repository(conn)
    await repository.ensure_user(USER)
    try:
        yield repository
    finally:
        await conn.close()


async def _product(repo: Repository) -> int:
    return await repo.add_product(
        user_id=USER,
        url="https://shop.example.com/kettle",
        name="Kettle",
        domain="shop.example.com",
        initial_price=Decimal("10"),
        currency="EUR",
    )


async def _row(repo: Repository, product_id: int | None) -> NotificationPrefs | None:
    row = await repo.get_notification_prefs(user_id=USER, product_id=product_id)
    return None if row is None else dataclasses.replace(row, updated_at=None)


async def _row_count(repo: Repository) -> int:
    cursor = await repo._conn.execute("SELECT COUNT(*) FROM notification_prefs")
    found = await cursor.fetchone()
    assert found is not None
    return int(found[0])


async def test_missing_global_row_is_created_with_only_the_changed_fields(
    repo: Repository,
) -> None:
    await update_prefs(repo, USER, None, throttle_per_hour=7)
    assert await _row(repo, None) == NotificationPrefs(user_id=USER, throttle_per_hour=7)


@pytest.mark.parametrize("field", sorted(NON_DEFAULT))
async def test_changing_one_field_keeps_every_other_field(repo: Repository, field: str) -> None:
    await repo.upsert_notification_prefs(NotificationPrefs(user_id=USER, **NON_DEFAULT))
    await update_prefs(repo, USER, None, **{field: OTHER_VALUE[field]})
    expected = NotificationPrefs(user_id=USER, **{**NON_DEFAULT, field: OTHER_VALUE[field]})
    assert await _row(repo, None) == expected


async def test_new_product_row_copies_digest_and_time_zone_of_the_global_row(
    repo: Repository,
) -> None:
    pid = await _product(repo)
    await repo.upsert_notification_prefs(
        NotificationPrefs(
            user_id=USER,
            digest_mode=True,
            digest_interval_minutes=30,
            timezone="Asia/Tokyo",
            quiet_hours_start="22:00",
            quiet_hours_end="08:00",
        )
    )
    await update_prefs(repo, USER, pid, mute=True)
    effective = await PreferencesManager(repo=repo).resolve(user_id=USER, product_id=pid)
    assert effective.mute is True
    assert effective.digest_mode is True
    assert effective.digest_interval_minutes == 30
    assert effective.timezone == "Asia/Tokyo"
    assert (effective.quiet_hours_start, effective.quiet_hours_end) == ("22:00", "08:00")
    global_row = await _row(repo, None)
    assert global_row is not None
    assert global_row.mute is False


async def test_existing_product_row_is_not_copied_over(repo: Repository) -> None:
    pid = await _product(repo)
    await repo.upsert_notification_prefs(
        NotificationPrefs(user_id=USER, product_id=pid, timezone="Europe/Berlin")
    )
    await repo.upsert_notification_prefs(
        NotificationPrefs(user_id=USER, digest_mode=True, timezone="Asia/Tokyo")
    )
    await update_prefs(repo, USER, pid, mute=True)
    row = await _row(repo, pid)
    assert row == NotificationPrefs(
        user_id=USER, product_id=pid, mute=True, timezone="Europe/Berlin"
    )


async def test_product_row_without_a_global_row_starts_from_the_defaults(
    repo: Repository,
) -> None:
    pid = await _product(repo)
    await update_prefs(repo, USER, pid, mute=True)
    assert await _row(repo, pid) == NotificationPrefs(user_id=USER, product_id=pid, mute=True)
    assert await _row(repo, None) is None


async def test_unknown_field_raises_before_any_write(repo: Repository) -> None:
    with pytest.raises(TypeError):
        await update_prefs(repo, USER, None, colour="red")
    assert await _row_count(repo) == 0


@pytest.mark.parametrize("field", ["user_id", "product_id", "updated_at"])
async def test_key_fields_cannot_be_changed(repo: Repository, field: str) -> None:
    with pytest.raises(TypeError):
        await update_prefs(repo, USER, None, **{field: 99})
    assert await _row_count(repo) == 0


@pytest.mark.parametrize(
    ("user_id", "product_id"),
    [
        (0, None),
        (-1, None),
        (True, None),
        ("10", None),
        (USER, 0),
        (USER, -1),
        (USER, True),
        (USER, "5"),
        (USER, 1.0),
    ],
)
async def test_ids_that_are_not_positive_integers_write_nothing(
    repo: Repository, user_id: Any, product_id: Any
) -> None:
    with pytest.raises((TypeError, ValueError)):
        await update_prefs(repo, user_id, product_id, mute=True)
    assert await _row_count(repo) == 0


# --- the commands and the buttons write through it ----------------------------


async def test_mute_command_on_a_product_keeps_the_global_digest_and_time_zone(
    repo: Repository,
) -> None:
    pid = await _product(repo)
    await repo.upsert_notification_prefs(
        NotificationPrefs(
            user_id=USER, digest_mode=True, digest_interval_minutes=30, timezone="Asia/Tokyo"
        )
    )
    await run_command(repo, USER, ["/mute", str(pid), "8"])
    effective = await PreferencesManager(repo=repo).resolve(user_id=USER, product_id=pid)
    assert effective.mute is True
    assert (effective.digest_mode, effective.digest_interval_minutes) == (True, 30)
    assert effective.timezone == "Asia/Tokyo"


async def test_unmute_command_on_a_product_keeps_the_global_digest_and_time_zone(
    repo: Repository,
) -> None:
    pid = await _product(repo)
    await repo.upsert_notification_prefs(
        NotificationPrefs(user_id=USER, mute=True, digest_mode=True, timezone="Asia/Tokyo")
    )
    await run_command(repo, USER, ["/mute", str(pid), "8"])
    await run_command(repo, USER, ["/unmute", str(pid)])
    row = await _row(repo, pid)
    assert row is not None
    assert (row.mute, row.mute_until) == (False, None)
    effective = await PreferencesManager(repo=repo).resolve(user_id=USER, product_id=pid)
    # Unmuting the product clears its own mute only; everything is still muted.
    assert (effective.mute, effective.mute_until) == (True, None)
    assert (effective.digest_mode, effective.timezone) == (True, "Asia/Tokyo")


@pytest.mark.parametrize("wire", ["s:dg:on", "s:dg:off"])
async def test_digest_button_keeps_the_interval(repo: Repository, wire: str) -> None:
    await repo.upsert_notification_prefs(
        NotificationPrefs(user_id=USER, digest_mode=True, digest_interval_minutes=30)
    )
    await press(repo, wire)
    row = await _row(repo, None)
    assert row is not None
    assert row.digest_interval_minutes == 30
    assert row.digest_mode is (wire == "s:dg:on")
