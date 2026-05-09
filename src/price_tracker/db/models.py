"""Dataclasses for DB rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal


@dataclass(frozen=True)
class UserRecord:
    user_id: int
    is_admin: bool
    is_active: bool
    display_name: str | None = None
    username: str | None = None


@dataclass(frozen=True)
class ProductRecord:
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


@dataclass(frozen=True)
class PriceHistoryRecord:
    id: int
    product_id: int
    price: Decimal
    checked_at: str


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
