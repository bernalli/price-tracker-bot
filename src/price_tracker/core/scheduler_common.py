"""Shared scheduler types, constants, and side-effect-free helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from price_tracker.core.exceptions import LISTING_GONE_STATUSES, ListingGone, ParseError
from price_tracker.core.health import HealthManager
from price_tracker.core.outlier import REQUIRED_CONFIRMATIONS

if TYPE_CHECKING:
    from price_tracker.core.alert import PriceAlert
    from price_tracker.core.registry import ScraperRegistry
    from price_tracker.db.repository import Repository
    from price_tracker.observability.metrics import MetricsRegistry

CHECK_TICK_MINUTES = 5
MAX_INTERVAL_MINUTES = 7 * 24 * 60


class NotifierFn(Protocol):
    """Delivers one formatted message to one user."""

    async def __call__(
        self,
        user_id: int,
        text: str,
        *,
        product_id: int | None = ...,
        payload: dict[str, Any] | None = ...,
    ) -> bool | None: ...


def _alert_payload(alert: PriceAlert, *, domain: str) -> dict[str, Any]:
    """Return the structured alert view consumed by preference-aware notifiers."""
    return {
        "kind": "price",
        "product_id": alert.product_id,
        "product_name": alert.product_name,
        "url": alert.url,
        "old_price": str(alert.old_price),
        "new_price": str(alert.new_price),
        "currency": alert.currency,
        "domain": domain,
    }


def _parse_db_timestamp(value: str) -> datetime:
    """Parse a SQLite or ISO-8601 timestamp and normalize it to UTC."""
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _failure_reason(exc: BaseException) -> tuple[str, str]:
    """Map a failed check to a supported ``(reason, detail)`` pair."""
    if isinstance(exc, ListingGone):
        return "listing_gone", f"HTTP {exc.status}"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in LISTING_GONE_STATUSES:
        return "listing_gone", f"HTTP {exc.response.status_code}"
    if isinstance(exc, ParseError):
        return "parse_error", str(exc)
    if isinstance(exc, httpx.HTTPError | ValueError | KeyError):
        return "http_error", str(exc)
    return "unexpected", str(exc)


def _no_op_health_mgr() -> HealthManager:
    """Return a HealthManager subclass that never locks or half-opens anything."""
    from price_tracker.core.health import QuarantineState  # noqa: PLC0415

    class _NoOpHealthManager(HealthManager):
        def __init__(self) -> None:
            pass

        def state(self, domain: str) -> QuarantineState:  # noqa: ARG002
            return QuarantineState.CLOSED

        def is_locked(self, domain: str) -> bool:  # noqa: ARG002
            return False

        def is_half_open(self, domain: str) -> bool:  # noqa: ARG002
            return False

        async def record_block(self, domain: str, *, reason: str) -> QuarantineState:  # noqa: ARG002
            return QuarantineState.CLOSED

        async def record_success(self, domain: str) -> QuarantineState:  # noqa: ARG002
            return QuarantineState.CLOSED

    return _NoOpHealthManager()


@dataclass
class SchedulerDeps:
    """Dependencies bundle for the Scheduler."""

    repo: Repository
    registry: ScraperRegistry
    client: httpx.AsyncClient
    notifier: NotifierFn
    max_consecutive_errors: int = 10
    listing_gone_confirmations: int = 3
    delay_between_products: float = 5.0
    notification_cooldown_hours: int = 24
    health_mgr: HealthManager = field(default_factory=_no_op_health_mgr)
    metrics: MetricsRegistry | None = None
    read_confirmations: int = REQUIRED_CONFIRMATIONS
    lang: str | None = None
    """Agreeing reads needed before an implausible price is trusted."""


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one product check in pull mode."""

    product_id: int
    user_id: int
    alert: PriceAlert | None = None
    disabled: bool = False
    reason: str | None = None
