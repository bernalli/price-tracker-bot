"""Dataclasses for DB rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
