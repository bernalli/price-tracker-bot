"""Bounded in-memory associations between list text messages and grid photos."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from price_tracker.bot.telegram_outcome import OutcomeKind
from price_tracker.core.image_url import normalize_image_url

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_GRID_ENTRIES: Final = 256
MAX_REFUSED_PREVIEWS: Final = 256
MAX_COOLDOWN_SECONDS: Final = 3600.0
MAX_TILES: Final = 6
MAX_MESSAGE_ID: Final = 2**63 - 1


def _message_id(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_MESSAGE_ID:
        raise ValueError("invalid message identifier")


@dataclass(frozen=True, slots=True)
class TileKey:
    """What one grid tile shows; a grid fingerprint is the tuple of its tiles' keys."""

    number: int
    product_id: int
    price: str
    image_url: str | None
    has_thumbnail: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.number, int)
            or isinstance(self.number, bool)
            or not 1 <= self.number <= MAX_TILES
            or not isinstance(self.product_id, int)
            or isinstance(self.product_id, bool)
            or not 1 <= self.product_id <= MAX_MESSAGE_ID
            or not isinstance(self.price, str)
            or not (self.image_url is None or isinstance(self.image_url, str))
            or not isinstance(self.has_thumbnail, bool)
        ):
            raise ValueError("invalid tile key")


GridFingerprint = tuple[TileKey, ...]
GridKey = tuple[int, int]


@dataclass(frozen=True, slots=True)
class GridEntry:
    """The grid photo above one list text message, and what it shows when known."""

    grid_message_id: int
    fingerprint: GridFingerprint | None


def _key(key: GridKey) -> None:
    if (
        not isinstance(key, tuple)
        or len(key) != 2
        or not isinstance(key[0], int)
        or isinstance(key[0], bool)
        or key[0] == 0
    ):
        raise ValueError("invalid grid key")
    _message_id(key[1])


def _fingerprint(value: GridFingerprint) -> None:
    if (
        not isinstance(value, tuple)
        or len(value) > MAX_TILES
        or any(
            not isinstance(tile, TileKey) or tile.number != number
            for number, tile in enumerate(value, 1)
        )
    ):
        raise ValueError("invalid grid fingerprint")


class GridRegistry:
    """In-memory map from list text messages to their grid photos; bounded, lost on restart."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_GRID_ENTRIES,
        max_refused_previews: int = MAX_REFUSED_PREVIEWS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        for value in (max_entries, max_refused_previews):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("registry limits must be positive integers")
        self._max_entries = max_entries
        self._max_refused_previews = max_refused_previews
        self._clock = clock
        self._entries: OrderedDict[GridKey, GridEntry] = OrderedDict()
        self._refused: OrderedDict[str, None] = OrderedDict()
        self._until = 0.0
        self._lock = asyncio.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    def __len__(self) -> int:
        return len(self._entries)

    def register(self, key: GridKey, grid_message_id: int, fingerprint: GridFingerprint) -> None:
        _key(key)
        _message_id(grid_message_id)
        _fingerprint(fingerprint)
        self._entries[key] = GridEntry(grid_message_id, fingerprint)
        self._entries.move_to_end(key)
        if len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def get(self, key: GridKey) -> GridEntry | None:
        _key(key)
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def forget(self, key: GridKey) -> None:
        _key(key)
        self._entries.pop(key, None)

    def apply_grid_outcome(
        self, key: GridKey, kind: OutcomeKind, fingerprint: GridFingerprint
    ) -> None:
        _key(key)
        if not isinstance(kind, OutcomeKind):
            raise ValueError("invalid outcome kind")
        _fingerprint(fingerprint)
        entry = self._entries.get(key)
        if entry is None:
            return
        if kind is OutcomeKind.REJECTED:
            del self._entries[key]
        else:
            self._entries[key] = GridEntry(
                entry.grid_message_id,
                fingerprint if kind is OutcomeKind.APPLIED else None,
            )

    def note_retry_after(self, seconds: float) -> None:
        if not isinstance(seconds, int | float) or isinstance(seconds, bool):
            raise ValueError("seconds must be a number")
        if seconds != seconds or seconds == float("inf"):
            duration = MAX_COOLDOWN_SECONDS
        else:
            duration = min(max(seconds, 0.0), MAX_COOLDOWN_SECONDS)
        self._until = max(self._until, self._clock() + duration)

    def cooling_down(self) -> bool:
        return self._clock() < self._until

    def refuse_preview(self, url: str) -> None:
        if not isinstance(url, str) or normalize_image_url(url, base_url="") != url:
            return
        self._refused[url] = None
        self._refused.move_to_end(url)
        if len(self._refused) > self._max_refused_previews:
            self._refused.popitem(last=False)

    def preview_refused(self, url: str) -> bool:
        return isinstance(url, str) and url in self._refused
