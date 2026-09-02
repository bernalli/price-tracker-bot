"""Pure data structures for grouping operational notices by user and domain."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from price_tracker.core.url_utils import extract_etld_plus_one

if TYPE_CHECKING:
    from decimal import Decimal

OPS_REACTIVATE_PREFIX = "ops_react_"
OPS_DELETE_PREFIX = "ops_del_"
OPS_DELETE_CONFIRM_PREFIX = "ops_delok_"
MAX_LISTED_PRODUCTS = 10

EventKind = Literal["suspended", "warning"]


def group_key_for(url: object) -> str:
    """Return eTLD+1, then lowercase netloc, then ``unknown`` without raising."""
    if not isinstance(url, str):
        return "unknown"
    try:
        registrable = extract_etld_plus_one(url)
        if registrable:
            return registrable.lower()
        netloc = urlparse(url).netloc.lower()
        return netloc or "unknown"
    except Exception:  # noqa: BLE001 - grouping must be total for malformed persisted URLs.
        return "unknown"


@dataclass(frozen=True)
class OperationalEvent:
    """One operational event emitted for a product during a sweep."""

    event: EventKind
    user_id: int
    product_id: int
    product_name: str
    url: str
    group_key: str
    reason: str
    detail: str | None
    last_error: str | None
    error_count: int
    max_errors: int
    last_price: Decimal | None
    currency: str | None
    last_checked_at: str | None


@dataclass(frozen=True)
class NoticeGroup:
    """Events of one kind for one user and domain, sorted by product identifier."""

    event: EventKind
    user_id: int
    group_key: str
    events: tuple[OperationalEvent, ...]

    @property
    def anchor_product_id(self) -> int:
        """Return the minimum product identifier for callback data only."""
        if not self.events:
            raise ValueError("notice group must contain at least one event")
        return self.events[0].product_id

    @property
    def primary_reason(self) -> str:
        """Return the most common reason, breaking ties alphabetically."""
        if not self.events:
            return "unknown"
        counts = Counter(event.reason for event in self.events)
        return min(counts, key=lambda reason: (-counts[reason], reason))


class NoticeCollector:
    """Accumulate last-write-wins operational events and expose stable groups."""

    def __init__(self) -> None:
        """Create an empty collector for one sweep."""
        self._events: dict[tuple[EventKind, int], OperationalEvent] = {}

    def add(self, event: OperationalEvent) -> None:
        """Store ``event``, replacing an earlier event of the same kind and product."""
        self._events[(event.event, event.product_id)] = event

    def groups(self) -> list[NoticeGroup]:
        """Return deterministically sorted groups with product-sorted event tuples."""
        grouped: dict[tuple[int, EventKind, str], list[OperationalEvent]] = {}
        for event in self._events.values():
            key = (event.user_id, event.event, event.group_key)
            grouped.setdefault(key, []).append(event)
        return [
            NoticeGroup(
                event=event_kind,
                user_id=user_id,
                group_key=group_key,
                events=tuple(sorted(events, key=lambda item: item.product_id)),
            )
            for (user_id, event_kind, group_key), events in sorted(grouped.items())
        ]

    def __len__(self) -> int:
        """Return the number of deduplicated events currently collected."""
        return len(self._events)
