"""A store refusing the bot up to the last quarantine tier is announced, and so is its reopening.

The first two quarantine tiers stay silent. At the end of every periodic sweep the
scheduler compares each store's state with the last notice each recipient accepted:
LOCKED_T3 and HALF_OPEN_T3 mean "blocked", CLOSED means "open". Recipients are the
active users with active products on the store. The last accepted notice lives in
``bot_config``, so a restart never repeats it, and a failed delivery is retried by
the next periodic sweep even when no product is due.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio
from freezegun import freeze_time
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from price_tracker.bot.messages import reset_locale, set_locale
from price_tracker.core.alert import (
    _escape_html,
    format_store_blocked_notice,
    format_store_reopened_notice,
)
from price_tracker.core.exceptions import HTTPBlockStatus
from price_tracker.core.health import HealthManager, QuarantineState
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scheduler_common import MAX_INTERVAL_MINUTES
from price_tracker.core.scheduler_notices import STORE_NOTICE_KEY_PREFIX, store_notice_key
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.core.textlimits import DOMAIN_BUDGET, truncate_visible
from price_tracker.core.url_utils import extract_etld_plus_one
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.models import ScraperHealth
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable

    import httpx

_MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")
DOMAIN = "store.com"
OTHER = "other.com"
_START = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
LAST_TIER = frozenset({QuarantineState.LOCKED_T3, QuarantineState.HALF_OPEN_T3})
EARLIER_TIERS = (
    QuarantineState.LOCKED_T1,
    QuarantineState.HALF_OPEN_T1,
    QuarantineState.LOCKED_T2,
    QuarantineState.HALF_OPEN_T2,
)
# Active products on store.com per recipient: user 1 has a paused third one, user 4
# was removed and user 3 only tracks another store.
RECIPIENT_COUNTS = {1: 2, 2: 1}
NOTICE_EVENTS = frozenset({"quarantine", "quarantine_end"})


class _SwitchScraper(AbstractScraper):
    """Refuses store.com while ``blocking`` is set; every other read succeeds."""

    name = "switch-stub"
    priority = 100

    def __init__(self) -> None:
        self.blocking = False
        self.calls = 0

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        self.calls += 1
        if self.blocking and extract_etld_plus_one(url) == DOMAIN:
            raise HTTPBlockStatus(status=429, url=url)
        return ProductInfo(price=Decimal("100"), currency="EUR")


@dataclass(frozen=True)
class _Delivery:
    user_id: int
    event: str
    domain: str
    count: int
    text: str


@dataclass
class _Recorder:
    """Notifier that records accepted store notices; it never asserts itself.

    ``_notify`` swallows exceptions, so a failed assertion in here would only look
    like a failed delivery: the tests inspect the recorded state instead.
    """

    deliveries: list[_Delivery] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    refuse_users: set[int] = field(default_factory=set)
    fail_next: int = 0
    attempts: int = 0
    failed: int = 0
    in_flight: int = 0
    max_in_flight: int = 0
    gate: asyncio.Event | None = None
    entered: asyncio.Event | None = None

    async def __call__(
        self,
        user_id: int,
        text: str,
        *,
        product_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        payload = payload or {}
        event = payload.get("event")
        self.texts.append(text)
        if event not in NOTICE_EVENTS:
            return True
        self.attempts += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.gate is not None:
                gate, self.gate = self.gate, None
                if self.entered is not None:
                    self.entered.set()
                await gate.wait()
            if user_id in self.refuse_users or self.fail_next > 0:
                self.fail_next = max(0, self.fail_next - 1)
                self.failed += 1
                return False
            self.deliveries.append(
                _Delivery(
                    user_id=user_id,
                    event=str(event),
                    domain=str(payload.get("domain")),
                    count=int(payload.get("count", 0)),
                    text=text,
                )
            )
            return True
        finally:
            self.in_flight -= 1

    def events(self, user_id: int, domain: str = DOMAIN) -> list[str]:
        return [d.event for d in self.deliveries if d.user_id == user_id and d.domain == domain]


@dataclass
class _World:
    conn: aiosqlite.Connection
    repo: Repository
    scraper: _SwitchScraper
    notifier: _Recorder
    products: dict[str, int]
    scheduler: Scheduler | None = None

    @property
    def sched(self) -> Scheduler:
        assert self.scheduler is not None
        return self.scheduler

    @property
    def health(self) -> HealthManager:
        return self.sched.deps.health_mgr


async def _boot(world: _World) -> Scheduler:
    """A fresh health manager and scheduler on the same database: a restart."""
    registry = ScraperRegistry()
    registry.register(world.scraper)
    health_mgr = HealthManager(world.repo)
    await health_mgr.load()
    world.scheduler = Scheduler(
        SchedulerDeps(
            repo=world.repo,
            registry=registry,
            client=AsyncMock(),  # the stub scraper never uses it
            notifier=world.notifier,
            max_consecutive_errors=99,
            delay_between_products=0.0,
            health_mgr=health_mgr,
        )
    )
    return world.scheduler


async def _add(repo: Repository, user_id: int, url: str) -> int:
    return await repo.add_product(
        user_id=user_id,
        url=url,
        name=f"Widget {url.rsplit('/', 1)[-1]}",
        domain=extract_etld_plus_one(url),
        initial_price=Decimal("100"),
        currency="EUR",
    )


@asynccontextmanager
async def _world() -> AsyncIterator[_World]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await apply_migrations(conn, _MIGRATIONS_DIR)
        repo = Repository(conn)
        for user_id in (1, 2, 3, 4):
            await repo.ensure_user(user_id)
        products = {
            "a": await _add(
                repo, 1, "https://store.com/products/a?diagnostic=SYNTHETIC_QUERY_MARKER"
            ),
            "b": await _add(repo, 1, "https://store.com/products/b"),
            "paused": await _add(repo, 1, "https://store.com/products/paused"),
            "c": await _add(repo, 2, "https://www.store.com/products/c"),
            "d": await _add(repo, 3, "https://other.com/products/d"),
            "e": await _add(repo, 4, "https://store.com/products/e"),
        }
        await repo.pause_product(products["paused"])
        await repo.remove_user(4)
        world = _World(conn, repo, _SwitchScraper(), _Recorder(), products)
        await _boot(world)
        yield world
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def world() -> AsyncIterator[_World]:
    async with _world() as built:
        yield built


async def _memory(world: _World, user_id: int, domain: str = DOMAIN) -> str | None:
    return await world.repo.get_config(store_notice_key(user_id, domain))


async def _memories(world: _World) -> dict[int, str | None]:
    return {uid: await _memory(world, uid) for uid in (1, 2, 3, 4)}


async def _refuse_until(world: _World, frozen: Any, targets: set[QuarantineState]) -> None:
    """Refuse store.com through real checks, waiting out each lockout, until a target state."""
    world.scraper.blocking = True
    for _attempt in range(30):
        if world.health.state(DOMAIN) in targets:
            return
        if world.health.is_locked(DOMAIN):
            until = world.health.locked_until(DOMAIN)
            assert until is not None
            frozen.move_to(until + timedelta(minutes=1))
            continue
        await world.sched.run_check_all()
    raise AssertionError(f"store.com never reached {targets}: {world.health.state(DOMAIN)}")


async def _answer(world: _World, frozen: Any) -> None:
    """Let store.com answer the next real check, waiting out a lockout first."""
    world.scraper.blocking = False
    if world.health.is_locked(DOMAIN):
        until = world.health.locked_until(DOMAIN)
        assert until is not None
        frozen.move_to(until + timedelta(minutes=1))
    await world.sched.run_check_all()
    assert world.health.state(DOMAIN) is QuarantineState.CLOSED


async def _idle_sweep(world: _World) -> None:
    """A periodic sweep in which no product is due: only the reconciliation runs."""
    now = datetime.now(UTC)
    for user in await world.repo.list_active_users():
        for product in await world.repo.list_products_for_user(
            user_id=user.user_id, only_active=True
        ):
            world.sched._attempted_at[product.id] = now
    calls = world.scraper.calls
    await world.sched.run_check_due(global_interval_minutes=MAX_INTERVAL_MINUTES)
    assert world.scraper.calls == calls, "an idle sweep must not check any product"


async def _lock_last_tier(health: HealthManager, domain: str = DOMAIN) -> None:
    """Twelve recorded refusals in a row reach the last tier without waiting."""
    for _block in range(12):
        await health.record_block(domain, reason="HTTP 429")
    assert health.state(domain) is QuarantineState.LOCKED_T3


def _summary(recorder: _Recorder) -> list[tuple[int, str, int]]:
    return [(d.user_id, d.event, d.count) for d in recorder.deliveries]


def _assert_alternation(recorder: _Recorder) -> None:
    """I1: per user and store, accepted notices alternate, starting with the block."""
    streams: dict[tuple[int, str], list[str]] = {}
    for delivery in recorder.deliveries:
        streams.setdefault((delivery.user_id, delivery.domain), []).append(delivery.event)
    for key, events in streams.items():
        expected = ["quarantine" if i % 2 == 0 else "quarantine_end" for i in range(len(events))]
        assert events == expected, key


# --- examples -------------------------------------------------------------------


async def test_only_the_last_tier_is_announced_once_to_each_owner(world: _World) -> None:
    with freeze_time(_START) as frozen:
        for tier in EARLIER_TIERS:
            await _refuse_until(world, frozen, {tier})
            await _idle_sweep(world)
            assert world.notifier.texts == [], tier
        await _refuse_until(world, frozen, {QuarantineState.LOCKED_T3})
        await _idle_sweep(world)
        await _refuse_until(world, frozen, {QuarantineState.HALF_OPEN_T3})
        await _idle_sweep(world)

    assert _summary(world.notifier) == [(1, "quarantine", 2), (2, "quarantine", 1)]
    assert {d.domain for d in world.notifier.deliveries} == {DOMAIN}
    sent = "\n".join(world.notifier.texts)
    assert "SYNTHETIC_QUERY_MARKER" not in sent
    assert "HTTP 429" not in sent
    assert await _memories(world) == {1: "blocked", 2: "blocked", 3: None, 4: None}


async def test_the_last_tier_refresh_and_a_restart_do_not_repeat_the_notice(
    world: _World,
) -> None:
    with freeze_time(_START) as frozen:
        await _refuse_until(world, frozen, {QuarantineState.LOCKED_T3})
        await _idle_sweep(world)
        assert len(world.notifier.deliveries) == 2

        for _renewal in range(2):
            await _refuse_until(world, frozen, {QuarantineState.HALF_OPEN_T3})
            await _idle_sweep(world)
            await world.sched.run_check_all()  # the probe is refused: LOCKED_T3 renewed
            assert world.health.state(DOMAIN) is QuarantineState.LOCKED_T3
            await _idle_sweep(world)
            await _idle_sweep(world)

        await _boot(world)
        assert world.health.state(DOMAIN) in LAST_TIER
        await _idle_sweep(world)
        await _idle_sweep(world)

    assert _summary(world.notifier) == [(1, "quarantine", 2), (2, "quarantine", 1)]


async def test_a_store_that_answers_again_is_announced_once(world: _World) -> None:
    with freeze_time(_START) as frozen:
        await _refuse_until(world, frozen, {QuarantineState.LOCKED_T3})
        await _idle_sweep(world)
        await _boot(world)

        await _answer(world, frozen)
        await _idle_sweep(world)
        await _idle_sweep(world)
        await _boot(world)
        await _idle_sweep(world)

        await _refuse_until(world, frozen, {QuarantineState.LOCKED_T1})
        await _idle_sweep(world)
        await _answer(world, frozen)
        await _idle_sweep(world)

    assert _summary(world.notifier) == [
        (1, "quarantine", 2),
        (2, "quarantine", 1),
        (1, "quarantine_end", 2),
        (2, "quarantine_end", 1),
    ]
    assert await _memories(world) == {1: "open", 2: "open", 3: None, 4: None}


async def test_a_reopening_needs_an_accepted_block_notice(world: _World) -> None:
    world.notifier.refuse_users = {2}
    with freeze_time(_START) as frozen:
        await _refuse_until(world, frozen, {QuarantineState.LOCKED_T3})
        await _idle_sweep(world)
        assert await _memory(world, 2) is None

        await _answer(world, frozen)
        world.notifier.refuse_users = set()
        await _idle_sweep(world)

    assert world.notifier.events(1) == ["quarantine", "quarantine_end"]
    assert world.notifier.events(2) == []
    assert await _memory(world, 2) is None


@pytest.mark.parametrize("notice", ["block", "reopening"])
async def test_failed_notices_retry_on_an_idle_periodic_sweep(world: _World, notice: str) -> None:
    with freeze_time(_START) as frozen:
        await _refuse_until(world, frozen, {QuarantineState.LOCKED_T3})
        if notice == "reopening":
            await _idle_sweep(world)
            await _answer(world, frozen)
        expected = "quarantine" if notice == "block" else "quarantine_end"
        before = await _memories(world)
        delivered = len(world.notifier.deliveries)

        world.notifier.fail_next = 2
        await _idle_sweep(world)
        assert world.notifier.failed == 2
        assert len(world.notifier.deliveries) == delivered
        assert await _memories(world) == before

        calls = world.scraper.calls
        await _idle_sweep(world)
        assert world.scraper.calls == calls
        assert [(d.user_id, d.event) for d in world.notifier.deliveries[delivered:]] == [
            (1, expected),
            (2, expected),
        ]
        stored = "blocked" if notice == "block" else "open"
        assert await _memories(world) == {1: stored, 2: stored, 3: None, 4: None}

        await _idle_sweep(world)
    assert len(world.notifier.deliveries) == delivered + 2


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param(
            {"locked_until": _START + timedelta(hours=12), "last_block_at": _START},
            id="locked_t3",
        ),
        pytest.param(
            {
                "locked_until": _START - timedelta(hours=1),
                "last_block_at": _START - timedelta(hours=25),
            },
            id="half_open_t3",
        ),
    ],
)
async def test_a_store_already_in_the_last_tier_is_announced_without_a_probe(
    world: _World, seed: dict[str, datetime]
) -> None:
    await world.repo.upsert_scraper_health(
        ScraperHealth(
            domain=DOMAIN,
            state=QuarantineState.LOCKED_T3.value,
            consecutive_blocks=12,
            locked_until=seed["locked_until"],
            last_block_at=seed["last_block_at"],
            last_block_reason="HTTP 429",
        )
    )
    with freeze_time(_START):
        await _boot(world)
        assert world.health.state(DOMAIN) in LAST_TIER
        await _idle_sweep(world)

    assert world.scraper.calls == 0
    assert _summary(world.notifier) == [(1, "quarantine", 2), (2, "quarantine", 1)]


async def test_manual_checks_are_reconciled_by_the_next_periodic_sweep(world: _World) -> None:
    with freeze_time(_START) as frozen:
        await _refuse_until(world, frozen, {QuarantineState.HALF_OPEN_T2})
        world.scraper.blocking = True
        await world.sched.check_one_product_for_user(product_id=world.products["a"], user_id=1)
        assert world.health.state(DOMAIN) is QuarantineState.LOCKED_T3
        assert world.notifier.deliveries == []

        await _idle_sweep(world)

    assert _summary(world.notifier) == [(1, "quarantine", 2), (2, "quarantine", 1)]


async def test_a_failing_reconciliation_never_fails_the_periodic_sweep(
    world: _World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    repo = world.repo
    real_users = repo.list_active_users
    reads = 0

    async def flaky_users() -> Any:
        nonlocal reads
        reads += 1
        if reads == 2:  # the reconciliation's own read, after the sweep's
            raise aiosqlite.OperationalError("database is locked")
        return await real_users()

    with freeze_time(_START) as frozen:
        monkeypatch.setattr(repo, "list_active_users", flaky_users)
        with caplog.at_level(logging.ERROR, logger="price_tracker.core.scheduler"):
            await world.sched.run_check_due(global_interval_minutes=5)
        monkeypatch.setattr(repo, "list_active_users", real_users)

        assert reads == 2
        assert world.scraper.calls == 4
        assert world.health.state(DOMAIN) is QuarantineState.CLOSED
        for key in ("a", "b", "c", "d"):
            record = await repo.get_product(world.products[key])
            assert record is not None
            assert record.consecutive_errors == 0
            assert record.last_error is None
        assert "Could not reconcile store notices" in caplog.text

        await _refuse_until(world, frozen, {QuarantineState.LOCKED_T3})
        real_get = repo.get_config
        failing = True

        async def get_config(key: str) -> str | None:
            if failing and key == store_notice_key(1, DOMAIN):
                raise aiosqlite.OperationalError("database is locked")
            return await real_get(key)

        monkeypatch.setattr(repo, "get_config", get_config)
        await _idle_sweep(world)
        assert [d.user_id for d in world.notifier.deliveries] == [2]
        assert await real_get(store_notice_key(1, DOMAIN)) is None
        assert await real_get(store_notice_key(2, DOMAIN)) == "blocked"

        failing = False
        await _idle_sweep(world)

    assert [d.user_id for d in world.notifier.deliveries] == [2, 1]
    assert await _memory(world, 1) == "blocked"


@pytest.mark.parametrize("direction", ["to_open", "to_blocked"])
async def test_a_state_change_during_delivery_converges_on_the_next_sweep(
    world: _World, direction: str
) -> None:
    health = world.health
    await _lock_last_tier(health)
    if direction == "to_open":
        prior: list[str] = []
        first_event, second_event = "quarantine", "quarantine_end"

        def transition() -> Awaitable[object]:
            return health.record_success(DOMAIN)

        final_state, final_memory = QuarantineState.CLOSED, "open"
    else:
        await world.sched._reconcile_store_notices()
        await health.record_success(DOMAIN)
        prior = ["quarantine"]
        first_event, second_event = "quarantine_end", "quarantine"

        def transition() -> Awaitable[object]:
            return _lock_last_tier(health)

        final_state, final_memory = QuarantineState.LOCKED_T3, "blocked"

    already = len(world.notifier.deliveries)
    gate, entered = asyncio.Event(), asyncio.Event()
    world.notifier.gate, world.notifier.entered = gate, entered
    first = asyncio.create_task(world.sched._reconcile_store_notices())
    await asyncio.wait_for(entered.wait(), timeout=5)

    await transition()
    assert health.state(DOMAIN) is final_state
    attempts = world.notifier.attempts
    second = asyncio.create_task(world.sched._reconcile_store_notices())
    for _yield in range(20):
        await asyncio.sleep(0)
    assert not second.done()
    assert world.sched._notice_lock.locked()
    assert world.notifier.attempts == attempts

    gate.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    assert world.notifier.max_in_flight == 1
    # The second reconciliation read the memory the first one wrote: it sent the
    # opposite notice, and only after every send of the first one.
    assert [d.event for d in world.notifier.deliveries[already:]] == [
        first_event,
        first_event,
        second_event,
        second_event,
    ]
    for uid in (1, 2):
        assert world.notifier.events(uid) == [*prior, first_event, second_event]
        assert await _memory(world, uid) == final_memory

    settled = len(world.notifier.deliveries)
    await world.sched._reconcile_store_notices()
    assert len(world.notifier.deliveries) == settled


async def test_a_user_removed_after_selection_is_not_notified(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _lock_last_tier(world.health)
    real_get_user = world.repo.get_user

    async def get_user(user_id: int) -> Any:
        # Rendering reads the recipient's language: the removal lands between the
        # selection of the candidates and the last authorization check.
        if user_id == 2:
            await world.repo.remove_user(2)
        return await real_get_user(user_id)

    monkeypatch.setattr(world.repo, "get_user", get_user)
    await world.sched._reconcile_store_notices()

    assert [d.user_id for d in world.notifier.deliveries] == [1]
    assert await _memory(world, 2) is None


_BLOCKED_TEXTS = {
    ("en", 1): (
        "🔒 <b>{d} is blocking the bot</b>\n\n1 product is not being updated.\n"
        "I keep retrying by myself and will tell you when it answers again.\n\n"
        "Details with /errori."
    ),
    ("en", 2): (
        "🔒 <b>{d} is blocking the bot</b>\n\n2 products are not being updated.\n"
        "I keep retrying by myself and will tell you when it answers again.\n\n"
        "Details with /errori."
    ),
    ("it", 1): (
        "🔒 <b>{d} blocca il bot</b>\n\n1 prodotto non viene aggiornato.\n"
        "Continuo a riprovare da solo e ti avviso quando risponde di nuovo.\n\n"
        "Dettagli con /errori."
    ),
    ("it", 2): (
        "🔒 <b>{d} blocca il bot</b>\n\n2 prodotti non vengono aggiornati.\n"
        "Continuo a riprovare da solo e ti avviso quando risponde di nuovo.\n\n"
        "Dettagli con /errori."
    ),
}
_REOPENED_TEXTS = {
    "en": "🔓 <b>{d} answers again</b>\n\nChecks on this store have resumed.",
    "it": "🔓 <b>{d} risponde di nuovo</b>\n\nI controlli su questo negozio sono ripresi.",
}


@pytest.mark.parametrize("lang", ["en", "it"])
@pytest.mark.parametrize("products", [1, 2])
@pytest.mark.parametrize(
    ("domain", "shown"),
    [("store.com", "store.com"), ('a<i>&"b.com', "a&lt;i&gt;&amp;&quot;b.com")],
)
def test_store_notices_are_translated_and_escaped(
    lang: str, products: int, domain: str, shown: str
) -> None:
    token = set_locale(lang)
    try:
        blocked = format_store_blocked_notice(domain=domain, products=products)
        reopened = format_store_reopened_notice(domain=domain)
    finally:
        reset_locale(token)
    assert blocked == _BLOCKED_TEXTS[(lang, products)].format(d=shown)
    assert reopened == _REOPENED_TEXTS[lang].format(d=shown)


# --- properties -----------------------------------------------------------------

_OPS = st.sampled_from(("refuse", "answer", "wait", "wait_short", "restart", "drop", "reconcile"))
_TO_LAST_TIER = ["refuse", "wait_short", "refuse", "wait", "refuse"]


async def _apply(world: _World, frozen: Any, op: str) -> None:
    if op == "refuse":
        world.scraper.blocking = True
        await world.sched.run_check_all()
    elif op == "answer":
        world.scraper.blocking = False
        await world.sched.run_check_all()
    elif op == "wait":
        frozen.tick(timedelta(hours=25))
    elif op == "wait_short":
        frozen.tick(timedelta(minutes=61))
    elif op == "restart":
        await _boot(world)
    elif op == "drop":
        world.notifier.fail_next += 1
    else:
        await world.sched._reconcile_store_notices()


async def _assert_converged(world: _World, state: QuarantineState) -> None:
    """L1/L2: after a clean reconciliation, memory matches the sampled state."""
    for uid in RECIPIENT_COUNTS:
        stored = await _memory(world, uid)
        if state in LAST_TIER:
            assert stored == "blocked", (uid, state)
        elif state is QuarantineState.CLOSED:
            assert stored != "blocked", (uid, state)


@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops=st.lists(_OPS, max_size=30))
@example(ops=[*_TO_LAST_TIER, "drop", "reconcile", "reconcile"])
@example(
    ops=[
        *_TO_LAST_TIER,
        "reconcile",
        "wait",
        "answer",
        "drop",
        "reconcile",
        "restart",
        "reconcile",
    ]
)
async def test_store_notices_reconcile_over_any_sequence(ops: list[str]) -> None:
    async with _world() as world:
        with freeze_time(_START) as frozen:
            for op in ops:
                delivered = len(world.notifier.deliveries)
                failed = world.notifier.failed
                memory = await _memories(world)
                state = world.health.state(DOMAIN)
                await _apply(world, frozen, op)

                _assert_alternation(world.notifier)
                assert all(d.user_id in RECIPIENT_COUNTS for d in world.notifier.deliveries)
                if op != "reconcile":
                    assert len(world.notifier.deliveries) == delivered, op
                    continue
                if state in EARLIER_TIERS:
                    assert len(world.notifier.deliveries) == delivered
                    assert await _memories(world) == memory
                elif world.notifier.failed == failed:
                    await _assert_converged(world, state)

            world.notifier.fail_next = 0
            state = world.health.state(DOMAIN)
            await world.sched._reconcile_store_notices()
            await _assert_converged(world, state)
            settled = len(world.notifier.deliveries)
            await world.sched._reconcile_store_notices()
            assert len(world.notifier.deliveries) == settled
            _assert_alternation(world.notifier)


_STORED = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.binary(),
    st.text(),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=12), st.text(max_size=12), max_size=3),
)


def _stored_examples(test: Any) -> Any:
    for stored in ("blocked", "open", "", "block", "blocked ", "BLOCKED"):
        for blocked in (True, False):
            test = example(stored=stored, blocked=blocked)(test)
    for blocked in (True, False):
        test = example(stored='{"state":"blocked","extra":1}', blocked=blocked)(test)
    return test


@settings(max_examples=80, deadline=None)
@given(stored=_STORED, blocked=st.booleans())
@_stored_examples
async def test_notice_memory_treats_only_the_exact_blocked_value_as_blocked(
    stored: object, blocked: bool
) -> None:
    async with _world() as world:
        if blocked:
            await _lock_last_tier(world.health)
        key = store_notice_key(1, DOMAIN)
        real_get = world.repo.get_config

        async def get_config(asked: str) -> object:
            return stored if asked == key else await real_get(asked)

        world.repo.get_config = get_config  # type: ignore[method-assign,assignment]
        try:
            await world.sched._reconcile_store_notices()
        finally:
            world.repo.get_config = real_get  # type: ignore[method-assign]

        sent = [d.event for d in world.notifier.deliveries if d.user_id == 1]
        expected = (stored == "blocked") != blocked
        if expected:
            assert sent == ["quarantine" if blocked else "quarantine_end"]
            assert await real_get(key) == ("blocked" if blocked else "open")
            settled = len(world.notifier.deliveries)
            await world.sched._reconcile_store_notices()
            assert len(world.notifier.deliveries) == settled
        else:
            assert sent == []
            assert await real_get(key) is None


_DOMAINS = st.one_of(
    st.text(),
    st.text(min_size=DOMAIN_BUDGET + 1, max_size=300),
    st.sampled_from(["", "ünïcödé.example", "<b>shop</b>", "a:b", "a:1:b", "x\ny", " "]),
)


@settings(max_examples=200, deadline=None)
@given(
    user_id=st.integers(min_value=1, max_value=2**53),
    domains=st.lists(_DOMAINS, min_size=1, max_size=5, unique=True),
)
def test_store_notice_keys_preserve_the_full_domain(user_id: int, domains: list[str]) -> None:
    keys = [store_notice_key(user_id, domain) for domain in domains]
    for domain, key in zip(domains, keys, strict=True):
        assert key == f"store_notice:{user_id}:{domain}"
        assert key.startswith(STORE_NOTICE_KEY_PREFIX)
    assert len(set(keys)) == len(domains)


@settings(max_examples=200, deadline=None)
@given(domain=st.text(max_size=300), products=st.integers(min_value=1, max_value=10_000))
@example(domain="<script>alert(1)</script>", products=1)
@example(domain="a&b.com", products=2)
@example(domain='"quoted".com', products=3)
@example(domain="a" * 59, products=1)
@example(domain="a" * 60, products=1)
@example(domain="a" * 61, products=1)
def test_store_notice_formatters_bound_and_escape_domains(domain: str, products: int) -> None:
    shown = _escape_html(truncate_visible(domain, DOMAIN_BUDGET))
    token = set_locale("en")
    try:
        blocked = format_store_blocked_notice(domain=domain, products=products)
        reopened = format_store_reopened_notice(domain=domain)
    finally:
        reset_locale(token)
    assert blocked.split("\n", 1)[0] == f"🔒 <b>{shown} is blocking the bot</b>"
    assert reopened.split("\n", 1)[0] == f"🔓 <b>{shown} answers again</b>"
    for text in (blocked, reopened):
        assert re.findall(r"<[^>]*>", text) == ["<b>", "</b>"]
    noun = "product is" if products == 1 else "products are"
    assert f"\n{products} {noun} not being updated." in blocked


_RAISE = object()
_UNRECOGNIZED = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=8), st.integers(), max_size=3),
    st.text(),
    st.sampled_from(
        [
            "LOCKED_T",
            "LOCKED_T4",
            "LOCKED_T3",
            "HALF_OPEN_T3",
            "CLOSED",
            json.dumps({"state": "LOCKED_T3"}),
        ]
    ),
    st.just(_RAISE),
)


@settings(max_examples=80, deadline=None)
@given(value=_UNRECOGNIZED)
@example(value=_RAISE)
async def test_unrecognized_health_states_do_not_create_notices(value: object) -> None:
    async with _world() as world:
        health = world.health
        # User 1 was told "blocked", user 2 nothing: a state wrongly read as open
        # would reach user 1, one wrongly read as blocked would reach user 2.
        await world.repo.set_config(store_notice_key(1, DOMAIN), "blocked")
        await _lock_last_tier(health, OTHER)
        real_state = health.state

        def state(domain: str) -> Any:
            if domain != DOMAIN:
                return real_state(domain)
            if value is _RAISE:
                raise ValueError("unreadable health state")
            return value

        health.state = state  # type: ignore[method-assign]
        try:
            await world.sched._reconcile_store_notices()
        finally:
            health.state = real_state  # type: ignore[method-assign]

        assert [d for d in world.notifier.deliveries if d.domain == DOMAIN] == []
        assert await _memory(world, 1) == "blocked"
        assert await _memory(world, 2) is None
        assert world.notifier.events(3, OTHER) == ["quarantine"]
        assert await _memory(world, 3, OTHER) == "blocked"
