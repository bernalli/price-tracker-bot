"""Dataclasses for DB rows."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal


class _DictCompatMixin:
    """Mapping-style access for legacy handlers still using dict semantics.

    The original ``bot.py`` monolith spoke to a row-as-``dict`` repository.
    Plan 1 F1 split bot.py into modules and switched the repository to typed
    ``@dataclass`` records, but most handlers still call ``record.get("key")``
    or ``record["key"]``. Until handlers are migrated to attribute access,
    this mixin keeps both APIs working without copying every row to a dict.
    """

    def __getitem__(self, key: str) -> Any:  # noqa: D401 — Mapping protocol
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return any(f.name == key for f in fields(self))  # type: ignore[arg-type]


@dataclass(frozen=True)
class UserRecord(_DictCompatMixin):
    user_id: int
    is_admin: bool
    is_active: bool
    display_name: str | None = None
    username: str | None = None


@dataclass(frozen=True)
class ProductRecord(_DictCompatMixin):
    id: int
    user_id: int
    url: str
    name: str | None
    domain: str | None
    initial_price: Decimal | None
    current_price: Decimal | None
    lowest_price: Decimal | None
    highest_price: Decimal | None
    target_price: Decimal | None
    threshold_type: str
    threshold_value: Decimal
    is_active: bool
    is_available: bool
    consecutive_errors: int
    currency: str
    check_interval_minutes: int | None
    last_checked_at: str | None
    last_notified_at: str | None
    pending_alert_price: Decimal | None = None
    pending_alert_at: str | None = None
    preferred_condition: str | None = None
    preferred_seller: str | None = None
    pending_read_price: Decimal | None = None
    pending_read_count: int = 0
    pending_read_streak: int = 0


@dataclass(frozen=True)
class PriceHistoryRecord(_DictCompatMixin):
    id: int
    product_id: int
    price: Decimal
    checked_at: str


@dataclass(frozen=True, slots=True)
class ProductErrorRow:
    """Lightweight projection for the /errori command (no full ProductRecord)."""

    id: int
    name: str | None
    url: str
    domain: str | None
    consecutive_errors: int
    last_error: str | None
    last_error_at: str | None


@dataclass(frozen=True, slots=True)
class ScraperHealth:
    """Persistent health state for a single eTLD+1 domain."""

    domain: str
    state: str = "CLOSED"
    consecutive_blocks: int = 0
    locked_until: datetime | None = None
    last_block_at: datetime | None = None
    last_block_reason: str | None = None
    last_success_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NotificationPrefs:
    """Per-user (and optional per-product) notification preferences.

    A row with ``product_id=None`` represents the user's global default.
    A row with ``product_id`` set overrides the global default for that product.
    """

    user_id: int
    product_id: int | None = None
    mute: bool = False
    mute_until: datetime | None = None
    digest_mode: bool = False
    digest_interval_minutes: int = 60
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    throttle_per_hour: int | None = None
    timezone: str = "Europe/Rome"
    throttle_state_json: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DigestEntry:
    """A queued alert waiting for digest flush."""

    id: int | None
    user_id: int
    product_id: int
    alert_payload_json: str
    enqueued_at: datetime | None = None
    flushed_at: datetime | None = None
