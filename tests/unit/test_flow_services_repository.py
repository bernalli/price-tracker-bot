"""``RepositoryFlowServices``: the coordinator's services on a real repository.

Every mutation re-checks, when the answer arrives, that the user is still
active and still sees the product (the admin sees every product, any other
user only their own), and writes only the closed set of ``(kind, value)``
pairs the prompts produce.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest
import pytest_asyncio

from price_tracker.app.inputs import (
    Absolute,
    AnyDrop,
    Cancel,
    ClearTarget,
    IntervalMinutes,
    Percentage,
    ResetInterval,
    SetTarget,
)
from price_tracker.bot.flow_services import RepositoryFlowServices
from price_tracker.bot.flows import ApplyStatus, FlowKind, PreparedProduct
from price_tracker.db.migrator import apply_migrations
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


async def test_product_name_is_truncated_to_sixty_characters(env: Env) -> None:
    product_id = await env.add_product(OWNER, "x" * 61)

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
