"""``RepositoryFlowServices``: the coordinator's services on a real repository.

Every mutation re-checks, when the answer arrives, that the user is still
active and still sees the product (the admin sees every product, any other
user only their own), and writes only the closed set of ``(kind, value)``
pairs the prompts produce.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest
import pytest_asyncio
from freezegun import freeze_time

from price_tracker.app.inputs import (
    Absolute,
    AnyDrop,
    Cancel,
    ClearTarget,
    Forever,
    IntervalMinutes,
    Off,
    Percentage,
    QuietHours,
    ResetInterval,
    SetTarget,
)
from price_tracker.bot.flow_services import RepositoryFlowServices
from price_tracker.bot.flows import ApplyStatus, FlowKind, PreparedProduct
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.models import NotificationPrefs
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "src" / "price_tracker" / "db" / "migrations"

OWNER = 10
OTHER = 11
ADMIN = 1
INACTIVE = 12
UNKNOWN = 99


class Env:
    """A migrated in-memory repository with users and two products."""

    def __init__(self, conn: aiosqlite.Connection, repo: Repository) -> None:
        self.conn = conn
        self.repo = repo
        self.services = RepositoryFlowServices(lambda: repo)
        self.product = 0
        self.other_product = 0

    async def add_product(self, user_id: int, name: str | None) -> int:
        return await self.repo.add_product(
            user_id=user_id,
            url=f"https://shop.example/item/{user_id}/{name}",
            name=name,
            domain="shop.example",
            initial_price=Decimal("100"),
            currency="EUR",
        )

    async def row(self, product_id: int) -> dict[str, Any]:
        product = await self.repo.get_product(product_id)
        assert product is not None
        return dataclasses.asdict(product)


@pytest_asyncio.fixture
async def env() -> AsyncIterator[Env]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(ADMIN, is_admin=True)
    await repo.ensure_user(OWNER)
    await repo.ensure_user(OTHER)
    await repo.ensure_user(INACTIVE)
    await repo.remove_user(INACTIVE)
    e = Env(conn, repo)
    e.product = await e.add_product(OWNER, "Kettle")
    e.other_product = await e.add_product(OTHER, "Fan")
    try:
        yield e
    finally:
        await conn.close()


@pytest.mark.parametrize(
    ("user_id", "expected"), [(OWNER, True), (ADMIN, True), (INACTIVE, False), (UNKNOWN, False)]
)
async def test_is_active_reads_the_user_row(env: Env, user_id: int, expected: bool) -> None:
    assert await env.services.is_active(user_id) is expected


@pytest.mark.parametrize(
    ("user_id", "expected"), [(ADMIN, True), (OWNER, False), (INACTIVE, False), (UNKNOWN, False)]
)
async def test_is_admin_needs_the_role_and_access(env: Env, user_id: int, expected: bool) -> None:
    assert await env.services.is_admin(user_id) is expected


async def test_a_deactivated_admin_is_not_an_admin(env: Env) -> None:
    await env.repo.remove_user(ADMIN)

    assert await env.repo.is_user_admin(ADMIN) is True
    assert await env.services.is_admin(ADMIN) is False


async def test_product_name_follows_visibility(env: Env) -> None:
    assert await env.services.product_name(OWNER, env.product) == "Kettle"
    assert await env.services.product_name(OTHER, env.product) is None
    assert await env.services.product_name(ADMIN, env.product) == "Kettle"
    assert await env.services.product_name(OWNER, 424242) is None
    assert await env.services.product_name(ADMIN, 424242) is None


@pytest.mark.parametrize("name", [None, ""])
async def test_product_name_without_a_name_is_the_id(env: Env, name: str | None) -> None:
    product_id = await env.add_product(OWNER, name)

    assert await env.services.product_name(OWNER, product_id) == f"#{product_id}"


async def test_product_name_over_sixty_cells_is_cut_with_an_ellipsis(env: Env) -> None:
    product_id = await env.add_product(OWNER, "x" * 61)

    assert await env.services.product_name(OWNER, product_id) == "x" * 59 + "…"


async def test_product_name_of_sixty_cells_is_kept_whole(env: Env) -> None:
    product_id = await env.add_product(OWNER, "x" * 60)

    assert await env.services.product_name(OWNER, product_id) == "x" * 60


VALID_WRITES: list[tuple[FlowKind, object, str, object]] = [
    (FlowKind.THRESHOLD, Percentage(20), "threshold", ("percentage", "20")),
    (FlowKind.THRESHOLD, Absolute(Decimal("5.50")), "threshold", ("absolute", "5.50")),
    (FlowKind.THRESHOLD, AnyDrop(), "threshold", ("any_drop", "0")),
    (FlowKind.TARGET, SetTarget(Decimal("49.90")), "target_price", "49.90"),
    (FlowKind.TARGET, ClearTarget(), "target_price", None),
    (FlowKind.INTERVAL, IntervalMinutes(30), "check_interval_minutes", 30),
    (FlowKind.INTERVAL, ResetInterval(), "check_interval_minutes", None),
]


def _observed(row: dict[str, Any], column: str) -> object:
    if column == "threshold":
        return (row["threshold_type"], str(row["threshold_value"]))
    value = row[column]
    return str(value) if isinstance(value, Decimal) else value


@pytest.mark.parametrize(("kind", "value", "column", "expected"), VALID_WRITES)
async def test_each_valid_pair_writes_its_column(
    env: Env, kind: FlowKind, value: object, column: str, expected: object
) -> None:
    await env.repo.set_target_price(env.product, Decimal("10"))
    await env.repo.set_product_interval(env.product, 60)

    status = await env.services.apply_value(OWNER, kind, env.product, value)

    assert status is ApplyStatus.OK
    assert _observed(await env.row(env.product), column) == expected


async def test_admin_writes_any_product(env: Env) -> None:
    status = await env.services.apply_value(
        ADMIN, FlowKind.INTERVAL, env.other_product, IntervalMinutes(45)
    )

    assert status is ApplyStatus.OK
    assert (await env.row(env.other_product))["check_interval_minutes"] == 45


_VALUES: list[object] = [
    Percentage(20),
    Absolute(Decimal("5")),
    AnyDrop(),
    SetTarget(Decimal("5")),
    ClearTarget(),
    IntervalMinutes(30),
    ResetInterval(),
    Cancel(),
]
_VALID = {(kind, type(value)) for kind, value, _, _ in VALID_WRITES}
MISMATCHED: list[tuple[FlowKind, object]] = [
    (kind, value)
    for kind in (FlowKind.THRESHOLD, FlowKind.TARGET, FlowKind.INTERVAL)
    for value in _VALUES
    if (kind, type(value)) not in _VALID
] + [
    (kind, value)
    for kind in (FlowKind.THRESHOLD, FlowKind.TARGET, FlowKind.INTERVAL, FlowKind.ADD)
    for value in (None, 20, "20", Decimal("20"))
]


def test_mismatched_pairs_cover_the_whole_cross_product() -> None:
    # 3 kinds x 8 values = 24, minus the 7 valid pairs, plus 4 kinds x 4 foreign values.
    assert len(MISMATCHED) == 24 - 7 + 16


@pytest.mark.parametrize(("kind", "value"), MISMATCHED, ids=repr)
async def test_mismatched_pair_raises_before_any_write(
    env: Env, kind: FlowKind, value: object
) -> None:
    before = env.conn.total_changes

    with pytest.raises(TypeError):
        await env.services.apply_value(OWNER, kind, env.product, value)

    assert env.conn.total_changes == before


async def test_inactive_user_is_not_authorised_and_nothing_is_written(env: Env) -> None:
    await env.repo.remove_user(OWNER)
    before = env.conn.total_changes

    status = await env.services.apply_value(OWNER, FlowKind.THRESHOLD, env.product, Percentage(20))

    assert status is ApplyStatus.NOT_AUTHORISED
    assert env.conn.total_changes == before


async def test_product_of_another_user_is_not_found_and_nothing_is_written(env: Env) -> None:
    before = env.conn.total_changes

    status = await env.services.apply_value(
        OWNER, FlowKind.THRESHOLD, env.other_product, Percentage(20)
    )

    assert status is ApplyStatus.NOT_FOUND
    assert env.conn.total_changes == before
    assert (await env.row(env.other_product))["threshold_type"] == "percentage"
    assert str((await env.row(env.other_product))["threshold_value"]) == "10"


async def test_deleted_product_is_not_found_and_nothing_is_written(env: Env) -> None:
    assert await env.repo.delete_product(env.product, user_id=OWNER)
    before = env.conn.total_changes

    status = await env.services.apply_value(OWNER, FlowKind.TARGET, env.product, ClearTarget())

    assert status is ApplyStatus.NOT_FOUND
    assert env.conn.total_changes == before


class _Untouchable:
    """A repository stand-in that fails the test on any attribute access."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"repository touched: {name}")


async def test_add_flow_methods_are_not_wired_and_touch_nothing() -> None:
    services = RepositoryFlowServices(lambda: _Untouchable())
    product = PreparedProduct(
        url="https://shop.example/x",
        name="x",
        store="shop.example",
        price=Decimal(1),
        currency=None,
    )

    with pytest.raises(RuntimeError, match="add flow is not wired"):
        await services.prepare_add(OWNER, "https://shop.example/x")
    with pytest.raises(RuntimeError, match="add flow is not wired"):
        await services.add_product(OWNER, product, "EUR")
    with pytest.raises(RuntimeError, match="add flow is not wired"):
        await services.add_scope_default(OWNER)
    with pytest.raises(RuntimeError, match="add flow is not wired"):
        await services.set_product_scope(OWNER, 1, cross_store=True, scope_override=None)


async def test_repository_is_read_at_call_time(env: Env) -> None:
    bot_data: dict[str, Any] = {}
    services = RepositoryFlowServices(lambda: bot_data["db"])

    bot_data["db"] = env.repo

    assert await services.is_active(OWNER) is True
    assert await services.product_name(OWNER, env.product) == "Kettle"


# --- apply_setting: the notification preferences the setting prompts write ------

SETTING_ROW: dict[str, Any] = {
    "mute": True,
    "mute_until": datetime(2030, 1, 1, tzinfo=UTC),
    "digest_mode": False,
    "digest_interval_minutes": 30,
    "quiet_hours_start": "23:00",
    "quiet_hours_end": "07:00",
    "throttle_per_hour": 5,
    "timezone": "Asia/Tokyo",
    "throttle_state_json": '{"ts": []}',
}
FROZEN_NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)
# (kind, value) -> the fields that answer writes, written from the prompt table.
SETTING_CASES: list[tuple[FlowKind, object, dict[str, Any]]] = [
    (FlowKind.MUTE, 8, {"mute": True, "mute_until": FROZEN_NOW + timedelta(hours=8)}),
    (FlowKind.MUTE, Forever(), {"mute": True, "mute_until": None}),
    (FlowKind.DIGEST, 45, {"digest_mode": True, "digest_interval_minutes": 45}),
    (
        FlowKind.QUIET,
        QuietHours(time(22, 0), time(8, 5)),
        {"quiet_hours_start": "22:00", "quiet_hours_end": "08:05"},
    ),
    (FlowKind.QUIET, Off(), {"quiet_hours_start": None, "quiet_hours_end": None}),
    (FlowKind.TIMEZONE, "America/New_York", {"timezone": "America/New_York"}),
    (FlowKind.THROTTLE, 12, {"throttle_per_hour": 12}),
    (FlowKind.THROTTLE, Off(), {"throttle_per_hour": None}),
]


async def _prefs(env: Env, user_id: int, product_id: int | None) -> NotificationPrefs | None:
    row = await env.repo.get_notification_prefs(user_id=user_id, product_id=product_id)
    return None if row is None else dataclasses.replace(row, updated_at=None)


async def _prefs_rows(env: Env) -> int:
    cursor = await env.conn.execute("SELECT COUNT(*) FROM notification_prefs")
    found = await cursor.fetchone()
    assert found is not None
    return int(found[0])


@pytest.mark.parametrize(("kind", "value", "fields"), SETTING_CASES, ids=repr)
async def test_each_setting_answer_changes_only_its_fields(
    env: Env, kind: FlowKind, value: object, fields: dict[str, Any]
) -> None:
    await env.repo.upsert_notification_prefs(NotificationPrefs(user_id=OWNER, **SETTING_ROW))
    with freeze_time(FROZEN_NOW):
        status = await env.services.apply_setting(OWNER, kind, None, value)
    assert status is ApplyStatus.OK
    expected = NotificationPrefs(user_id=OWNER, **{**SETTING_ROW, **fields})
    assert await _prefs(env, OWNER, None) == expected


async def test_a_timezone_answer_is_stored_exactly(env: Env) -> None:
    await env.services.apply_setting(
        OWNER, FlowKind.TIMEZONE, None, "America/Argentina/Buenos_Aires"
    )
    row = await _prefs(env, OWNER, None)
    assert row is not None
    assert row.timezone == "America/Argentina/Buenos_Aires"


async def test_a_digest_interval_turns_the_digest_on(env: Env) -> None:
    await env.services.apply_setting(OWNER, FlowKind.DIGEST, None, 15)
    row = await _prefs(env, OWNER, None)
    assert row is not None
    assert (row.digest_mode, row.digest_interval_minutes) == (True, 15)


async def test_a_product_mute_writes_the_product_row_with_the_global_digest_and_zone(
    env: Env,
) -> None:
    await env.repo.upsert_notification_prefs(NotificationPrefs(user_id=OWNER, **SETTING_ROW))
    with freeze_time(FROZEN_NOW):
        status = await env.services.apply_setting(OWNER, FlowKind.MUTE, env.product, 3)
    assert status is ApplyStatus.OK
    row = await _prefs(env, OWNER, env.product)
    assert row == NotificationPrefs(
        user_id=OWNER,
        product_id=env.product,
        mute=True,
        mute_until=FROZEN_NOW + timedelta(hours=3),
        digest_mode=False,
        digest_interval_minutes=30,
        timezone="Asia/Tokyo",
    )
    assert await _prefs(env, OWNER, None) == NotificationPrefs(user_id=OWNER, **SETTING_ROW)


@pytest.mark.parametrize(
    ("kind", "product", "value"),
    [
        (FlowKind.THROTTLE, "own", 5),
        (FlowKind.DIGEST, "own", 30),
        (FlowKind.MUTE, None, "12"),
        (FlowKind.MUTE, None, True),
        (FlowKind.MUTE, None, Off()),
        (FlowKind.DIGEST, None, Off()),
        (FlowKind.QUIET, None, "22:00-08:00"),
        (FlowKind.TIMEZONE, None, 5),
        (FlowKind.THROTTLE, None, Forever()),
        (FlowKind.DEBUG, None, "https://shop.example"),
        (FlowKind.THRESHOLD, None, Percentage(10)),
    ],
    ids=repr,
)
async def test_a_pair_no_prompt_produces_raises_before_any_write(
    env: Env, kind: FlowKind, product: str | None, value: object
) -> None:
    product_id = env.product if product == "own" else None
    with pytest.raises(TypeError):
        await env.services.apply_setting(OWNER, kind, product_id, value)
    assert await _prefs_rows(env) == 0


async def test_an_admin_cannot_mute_the_product_of_another_user(env: Env) -> None:
    status = await env.services.apply_setting(ADMIN, FlowKind.MUTE, env.product, 8)
    assert status is ApplyStatus.NOT_FOUND
    assert await _prefs_rows(env) == 0


async def test_a_missing_product_is_not_found(env: Env) -> None:
    status = await env.services.apply_setting(OWNER, FlowKind.MUTE, 999_999, 8)
    assert status is ApplyStatus.NOT_FOUND
    assert await _prefs_rows(env) == 0


async def test_a_user_deactivated_before_answering_writes_nothing(env: Env) -> None:
    await env.repo.remove_user(OWNER)
    for kind, value, _fields in SETTING_CASES:
        assert await env.services.apply_setting(OWNER, kind, None, value) is (
            ApplyStatus.NOT_AUTHORISED
        )
    status = await env.services.apply_setting(OWNER, FlowKind.MUTE, env.product, 8)
    assert status is ApplyStatus.NOT_AUTHORISED
    assert await _prefs_rows(env) == 0
