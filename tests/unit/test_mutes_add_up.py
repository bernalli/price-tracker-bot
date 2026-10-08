"""A product mute and the mute of everything add up.

A product is muted while its own mute is active or while everything is muted; it stays
muted until the later of the two ends, and a mute without an end wins. Unmuting a product
clears only its own mute: it never makes the product an exception to muting everything.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import pytest
import pytest_asyncio
from freezegun import freeze_time
from hypothesis import given, settings
from hypothesis import strategies as st

import price_tracker
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.models import NotificationPrefs
from price_tracker.db.repository import Repository
from price_tracker.notifier.preferences import PreferencesManager, is_muted_now
from tests.unit.test_nav_dispatch import (
    USER,
    add_product,
    all_prefs_rows,
    global_row,
    press,
    run_command,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path(price_tracker.__file__).resolve().parent / "db" / "migrations"
T0 = datetime(2026, 3, 1, 12, tzinfo=UTC)
ROUTES = ["button", "command"]


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


async def _product_muted(repo: Repository, product_id: int, now: datetime) -> bool:
    effective = await PreferencesManager(repo=repo).resolve(user_id=USER, product_id=product_id)
    return is_muted_now(effective, now_utc=now)


async def _everything_muted(repo: Repository, now: datetime) -> bool:
    effective = await PreferencesManager(repo=repo).resolve_global(user_id=USER)
    return is_muted_now(effective, now_utc=now)


async def _unmute_product(repo: Repository, route: str, product_id: int) -> None:
    if route == "button":
        await press(repo, f"p:{product_id}:mu:off")
    else:
        await run_command(repo, USER, ["/unmute", str(product_id)])


async def test_an_expired_product_mute_does_not_hide_muting_everything(repo: Repository) -> None:
    pid = await add_product(repo, USER, "Kettle")
    with freeze_time(T0, real_asyncio=True):
        await press(repo, f"p:{pid}:mu:1")
    later = T0 + timedelta(hours=2)
    with freeze_time(later, real_asyncio=True):
        await press(repo, "s:mu:0")

    assert await _everything_muted(repo, later)
    assert await _product_muted(repo, pid, later)


@pytest.mark.parametrize("route", ROUTES)
async def test_unmuting_a_product_does_not_exempt_it_from_muting_everything(
    repo: Repository, route: str
) -> None:
    pid = await add_product(repo, USER, "Kettle")
    await _unmute_product(repo, route, pid)
    with freeze_time(T0, real_asyncio=True):
        await press(repo, "s:mu:0")

    assert await _product_muted(repo, pid, T0)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("everything_muted", [False, True])
async def test_unmuting_a_product_without_its_own_row_writes_no_row(
    repo: Repository, route: str, everything_muted: bool
) -> None:
    pid = await add_product(repo, USER, "Kettle")
    if everything_muted:
        await press(repo, "s:mu:0")
    rows_before = await all_prefs_rows(repo)
    shared_before = await global_row(repo, USER)

    await _unmute_product(repo, route, pid)

    assert len(await all_prefs_rows(repo)) == len(rows_before)
    assert await repo.get_notification_prefs(user_id=USER, product_id=pid) is None
    assert await global_row(repo, USER) == shared_before
    assert await _product_muted(repo, pid, datetime.now(UTC)) is everything_muted


@pytest.mark.parametrize("route", ROUTES)
async def test_unmuting_a_product_clears_only_its_own_mute(repo: Repository, route: str) -> None:
    pid = await add_product(repo, USER, "Kettle")
    with freeze_time(T0, real_asyncio=True):
        await press(repo, "s:mu:0")
        await press(repo, f"p:{pid}:mu:8")
        await _unmute_product(repo, route, pid)

    row = await repo.get_notification_prefs(user_id=USER, product_id=pid)
    assert row is not None
    assert (row.mute, row.mute_until) == (False, None)
    assert await _everything_muted(repo, T0)
    assert await _product_muted(repo, pid, T0)


async def test_a_product_muted_longer_than_everything_stays_muted_after_it(
    repo: Repository,
) -> None:
    pid = await add_product(repo, USER, "Kettle")
    with freeze_time(T0, real_asyncio=True):
        await press(repo, "s:mu:1")
        await press(repo, f"p:{pid}:mu:8")

    assert await _product_muted(repo, pid, T0 + timedelta(hours=4))
    assert not await _product_muted(repo, pid, T0 + timedelta(hours=9))


# --- property: any product mute, any global mute, any instant ------------------------

PRODUCT = 7
# A stored mute: no row at all, or (mute flag, end in minutes from T0 or None for no end).
MuteState = tuple[bool, int | None] | None
mute_states = st.one_of(
    st.none(), st.tuples(st.booleans(), st.one_of(st.none(), st.integers(-600, 600)))
)


def _row(state: MuteState, product_id: int | None) -> NotificationPrefs | None:
    if state is None:
        return None
    flag, minutes = state
    end = None if minutes is None else T0 + timedelta(minutes=minutes)
    if product_id is None:
        return NotificationPrefs(
            user_id=USER,
            mute=flag,
            mute_until=end,
            digest_mode=False,
            digest_interval_minutes=90,
            quiet_hours_start="22:00",
            quiet_hours_end="07:00",
            throttle_per_hour=4,
            timezone="Europe/Berlin",
        )
    return NotificationPrefs(
        user_id=USER,
        product_id=product_id,
        mute=flag,
        mute_until=end,
        digest_mode=True,
        digest_interval_minutes=30,
        timezone="Asia/Tokyo",
    )


def _oracle(own: MuteState, shared: MuteState, now: datetime) -> tuple[bool, datetime | None]:
    """Muted while either mute is active; until the later active end, no end winning."""
    active_ends: list[datetime | None] = []
    for state in (own, shared):
        if state is None or not state[0]:
            continue
        end = None if state[1] is None else T0 + timedelta(minutes=state[1])
        if end is None or now < end:
            active_ends.append(end)
    if not active_ends:
        return False, None
    if None in active_ends:
        return True, None
    return True, max(end for end in active_ends if end is not None)


class _Rows:
    """Preference rows read by the manager, keyed by product id (None for the global row)."""

    def __init__(self, own: NotificationPrefs | None, shared: NotificationPrefs | None) -> None:
        self._rows = {PRODUCT: own, None: shared}

    async def get_notification_prefs(
        self, *, user_id: int, product_id: int | None
    ) -> NotificationPrefs | None:
        assert user_id == USER
        return self._rows[product_id]


@settings(max_examples=400, deadline=None)
@given(own=mute_states, shared=mute_states, offset=st.integers(-700, 700))
def test_product_and_global_mutes_add_up(own: MuteState, shared: MuteState, offset: int) -> None:
    now = T0 + timedelta(minutes=offset)
    rows = _Rows(_row(own, PRODUCT), _row(shared, None))
    manager = PreferencesManager(repo=rows)  # type: ignore[arg-type]
    effective = asyncio.run(manager.resolve(user_id=USER, product_id=PRODUCT))

    muted, until = _oracle(own, shared, now)
    assert is_muted_now(effective, now_utc=now) is muted
    if muted:
        assert effective.mute_until == until
    # The other fields keep resolving product row first, then the global row.
    if own is not None:
        assert (effective.digest_mode, effective.digest_interval_minutes) == (True, 30)
        assert effective.timezone == "Asia/Tokyo"
    elif shared is not None:
        assert (effective.digest_mode, effective.digest_interval_minutes) == (False, 90)
        assert effective.timezone == "Europe/Berlin"
    else:
        assert effective.timezone == "Europe/Rome"
    quiet = (None, None) if shared is None else ("22:00", "07:00")
    assert (effective.quiet_hours_start, effective.quiet_hours_end) == quiet
    assert effective.throttle_per_hour == (None if shared is None else 4)


@settings(max_examples=200, deadline=None)
@given(shared=mute_states, offset=st.integers(-700, 700))
def test_everything_muted_ignores_product_rows(shared: MuteState, offset: int) -> None:
    now = T0 + timedelta(minutes=offset)
    rows = _Rows(_row((True, None), PRODUCT), _row(shared, None))
    manager = PreferencesManager(repo=rows)  # type: ignore[arg-type]
    effective = asyncio.run(manager.resolve_global(user_id=USER))

    assert is_muted_now(effective, now_utc=now) is _oracle(None, shared, now)[0]
