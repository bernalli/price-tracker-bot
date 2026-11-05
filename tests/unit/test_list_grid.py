"""Grid associations, cooldowns and preview refusals follow bounded state models."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from price_tracker.bot.list_grid import GridEntry, GridRegistry, TileKey
from price_tracker.bot.telegram_outcome import OutcomeKind

if TYPE_CHECKING:
    from collections.abc import Callable


def fingerprint(product_id: int = 1) -> tuple[TileKey, ...]:
    return (TileKey(1, product_id, "€12.00", None, False),)


def snapshot(registry: GridRegistry) -> tuple[Any, ...]:
    return tuple(registry._entries.items())


@pytest.mark.parametrize("limit", ["max_entries", "max_refused_previews"])
@pytest.mark.parametrize("value", [True, False, None, 0, -1, 1.5, "2"])
def test_invalid_capacities_are_refused(limit: str, value: Any) -> None:
    with pytest.raises(ValueError, match="positive"):
        GridRegistry(**{limit: value})


@pytest.mark.parametrize(
    "key",
    [
        None,
        (),
        (1,),
        (1, 2, 3),
        [1, 1],
        (True, 1),
        (0, 1),
        ("1", 1),
        (1.0, 1),
        (1, True),
        (1, 0),
        (1, -1),
        (1, 2**63),
        (1, 1.0),
        (1, "1"),
    ],
)
@pytest.mark.parametrize("operation", ["get", "register", "forget", "outcome"])
def test_invalid_keys_never_change_state(key: Any, operation: str) -> None:
    registry = GridRegistry()
    registry.register((7, 1), 2, fingerprint())
    before = snapshot(registry)
    calls: dict[str, Callable[[], object]] = {
        "get": lambda: registry.get(key),
        "register": lambda: registry.register(key, 2, fingerprint()),
        "forget": lambda: registry.forget(key),
        "outcome": lambda: registry.apply_grid_outcome(key, OutcomeKind.APPLIED, fingerprint()),
    }
    with pytest.raises(ValueError, match="key|identifier"):
        calls[operation]()
    assert snapshot(registry) == before


@pytest.mark.parametrize("message_id", [True, False, None, 0, -1, 2**63, 1.0, "1"])
def test_invalid_photo_identifiers_do_not_replace_entries(message_id: Any) -> None:
    registry = GridRegistry()
    registry.register((-7, 1), 2, fingerprint())
    before = snapshot(registry)
    with pytest.raises(ValueError, match="identifier"):
        registry.register((-7, 1), message_id, fingerprint())
    assert snapshot(registry) == before


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        [TileKey(1, 1, "x", None, False)],
        tuple(TileKey(n, n, "x", None, False) for n in range(1, 7))
        + (TileKey(1, 7, "x", None, False),),
        (TileKey(2, 1, "x", None, False), TileKey(1, 2, "x", None, False)),
        (TileKey(1, 1, "x", None, False), TileKey(1, 2, "x", None, False)),
        (TileKey(1, 1, "x", None, False), TileKey(3, 2, "x", None, False)),
        (object(),),
    ],
)
@pytest.mark.parametrize("operation", ["register", "outcome"])
def test_invalid_fingerprints_are_rejected_before_any_state_change(
    value: Any, operation: str
) -> None:
    registry = GridRegistry()
    registry.register((1, 1), 2, fingerprint())
    before = snapshot(registry)
    call: Callable[[], object] = (
        (lambda: registry.register((1, 1), 3, value))
        if operation == "register"
        else (lambda: registry.apply_grid_outcome((1, 1), OutcomeKind.APPLIED, value))
    )
    with pytest.raises(ValueError, match="fingerprint"):
        call()
    assert snapshot(registry) == before


@pytest.mark.parametrize(
    "fields",
    [
        {"number": True},
        {"number": 0},
        {"number": 7},
        {"number": "1"},
        {"product_id": False},
        {"product_id": 0},
        {"product_id": 2**63},
        {"product_id": 1.0},
        {"price": None},
        {"image_url": False},
        {"has_thumbnail": 1},
    ],
)
def test_tile_key_fields_are_strict(fields: dict[str, Any]) -> None:
    valid = {"number": 1, "product_id": 1, "price": "x", "image_url": None, "has_thumbnail": False}
    with pytest.raises(ValueError, match="tile"):
        TileKey(**dict(valid, **fields))


def test_register_get_lru_and_restart() -> None:
    registry = GridRegistry(max_entries=2)
    registry.register((1, 1), 10, fingerprint(1))
    registry.register((1, 2), 20, fingerprint(2))
    assert registry.get((1, 1)) == GridEntry(10, fingerprint(1))
    registry.register((1, 3), 30, fingerprint(3))
    assert registry.get((1, 2)) is None
    assert len(registry) == 2
    registry.register((1, 1), 11, ())
    assert registry.get((1, 1)) == GridEntry(11, ())
    assert GridRegistry().get((1, 1)) is None
    assert isinstance(registry.lock, asyncio.Lock)
    assert registry.lock is registry.lock


@pytest.mark.parametrize("kind", list(OutcomeKind))
def test_outcomes_never_resurrect_an_absent_key(kind: OutcomeKind) -> None:
    registry = GridRegistry()
    registry.apply_grid_outcome((1, 1), kind, fingerprint())
    assert len(registry) == 0
    registry.register((1, 1), 10, fingerprint())
    registry.apply_grid_outcome((1, 1), kind, fingerprint(2))
    entry = registry.get((1, 1))
    if kind is OutcomeKind.REJECTED:
        assert entry is None
    else:
        assert entry == GridEntry(10, fingerprint(2) if kind is OutcomeKind.APPLIED else None)


def test_invalid_outcome_kinds_are_refused_without_mutation() -> None:
    registry = GridRegistry()
    registry.register((1, 1), 10, fingerprint())
    before = snapshot(registry)
    invalid: Any = "applied"
    with pytest.raises(ValueError, match="kind"):
        registry.apply_grid_outcome((1, 1), invalid, fingerprint())
    assert snapshot(registry) == before


@pytest.mark.parametrize(
    ("seconds", "duration"),
    [
        (0, 0),
        (5.5, 5.5),
        (-1, 0),
        (-math.inf, 0),
        (math.nan, 3600),
        (math.inf, 3600),
        (10**9, 3600),
    ],
)
def test_retry_after_limits_and_longest_cooldown(seconds: float, duration: float) -> None:
    clock = [100.0]
    registry = GridRegistry(clock=lambda: clock[0])
    registry.note_retry_after(seconds)
    assert registry.cooling_down() is (duration > 0)
    if duration > 0:
        clock[0] += duration - 0.5
        assert registry.cooling_down()
        registry.note_retry_after(-1)
        assert registry.cooling_down()
        clock[0] += 0.5
    assert not registry.cooling_down()
    registry.note_retry_after(100)
    registry.note_retry_after(1)
    clock[0] += 99
    assert registry.cooling_down()
    clock[0] += 1
    assert not registry.cooling_down()


@pytest.mark.parametrize("seconds", [True, False, "5", None, {}, []])
def test_bad_cooldowns_do_not_change_the_previous_pause(seconds: Any) -> None:
    clock = [0.0]
    registry = GridRegistry(clock=lambda: clock[0])
    registry.note_retry_after(5)
    with pytest.raises(ValueError, match="seconds"):
        registry.note_retry_after(seconds)
    clock[0] = 5
    assert not registry.cooling_down()


def test_refused_previews_are_canonical_bounded_and_refreshed() -> None:
    registry = GridRegistry(max_refused_previews=2)
    one, two, three = ("https://shop.com/1.jpg", "https://shop.com/2.jpg", "https://shop.com/3.jpg")
    for bad in (
        "http://shop.com/a.jpg",
        "https://shop.com/a.jpg#part",
        " https://shop.com/a.jpg",
        "/relative",
        "https://[bad",
    ):
        registry.refuse_preview(bad)
        assert not registry.preview_refused(bad)
    registry.refuse_preview(one)
    registry.refuse_preview(two)
    registry.refuse_preview(one)
    registry.refuse_preview(three)
    assert registry.preview_refused(one)
    assert registry.preview_refused(three)
    assert not registry.preview_refused(two)
    default = GridRegistry()
    for index in range(257):
        default.refuse_preview(f"https://shop.com/{index}.jpg")
    assert not default.preview_refused("https://shop.com/0.jpg")
    assert default.preview_refused("https://shop.com/256.jpg")


class RegistryMachine(RuleBasedStateMachine):
    @initialize(capacity=st.integers(1, 4))
    def setup(self, capacity: int) -> None:
        self.capacity = capacity
        self.clock = 0.0
        self.until = 0.0
        self.registry = GridRegistry(
            max_entries=capacity, max_refused_previews=3, clock=lambda: self.clock
        )
        self.model: OrderedDict[tuple[int, int], GridEntry] = OrderedDict()
        self.refused: OrderedDict[str, None] = OrderedDict()

    @rule(message_id=st.integers(1, 6), product_id=st.integers(1, 6))
    def register(self, message_id: int, product_id: int) -> None:
        key = (1, message_id)
        value = GridEntry(10 + message_id, fingerprint(product_id))
        self.registry.register(key, value.grid_message_id, fingerprint(product_id))
        self.model[key] = value
        self.model.move_to_end(key)
        if len(self.model) > self.capacity:
            self.model.popitem(last=False)

    @rule(message_id=st.integers(1, 6))
    def get(self, message_id: int) -> None:
        key = (1, message_id)
        assert self.registry.get(key) == self.model.get(key)
        if key in self.model:
            self.model.move_to_end(key)

    @rule(message_id=st.integers(1, 6))
    def forget(self, message_id: int) -> None:
        key = (1, message_id)
        self.registry.forget(key)
        self.model.pop(key, None)

    @rule(
        message_id=st.integers(1, 6),
        kind=st.sampled_from(list(OutcomeKind)),
        product_id=st.integers(1, 6),
    )
    def outcome(self, message_id: int, kind: OutcomeKind, product_id: int) -> None:
        key = (1, message_id)
        self.registry.apply_grid_outcome(key, kind, fingerprint(product_id))
        if key in self.model:
            if kind is OutcomeKind.REJECTED:
                del self.model[key]
            else:
                self.model[key] = GridEntry(
                    self.model[key].grid_message_id,
                    fingerprint(product_id) if kind is OutcomeKind.APPLIED else None,
                )

    @rule(seconds=st.sampled_from([0, -1, 5.5, 100, math.nan, math.inf]))
    def cooldown(self, seconds: float) -> None:
        self.registry.note_retry_after(seconds)
        duration = (
            3600 if math.isnan(seconds) or seconds == math.inf else max(0, min(seconds, 3600))
        )
        self.until = max(self.until, self.clock + duration)

    @rule(seconds=st.integers(0, 100))
    def advance(self, seconds: int) -> None:
        self.clock += seconds

    @rule(index=st.integers(1, 6))
    def preview(self, index: int) -> None:
        url = f"https://shop.com/{index}.jpg"
        self.registry.refuse_preview(url)
        self.refused[url] = None
        self.refused.move_to_end(url)
        if len(self.refused) > 3:
            self.refused.popitem(last=False)

    @invariant()
    def state_matches(self) -> None:
        assert snapshot(self.registry) == tuple(self.model.items())
        assert len(self.registry) <= self.capacity
        assert self.registry.cooling_down() is (self.clock < self.until)
        for index in range(1, 7):
            url = f"https://shop.com/{index}.jpg"
            assert self.registry.preview_refused(url) is (url in self.refused)


TestRegistryMachine = RegistryMachine.TestCase
TestRegistryMachine.settings = settings(max_examples=200, stateful_step_count=60, deadline=None)
