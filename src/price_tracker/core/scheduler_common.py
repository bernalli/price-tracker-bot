"""Shared scheduler types, constants, and side-effect-free helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from price_tracker.core.domain_spacing import DomainSpacing
from price_tracker.core.drop_floor import NO_DROP_FLOOR, DropFloor
from price_tracker.core.exceptions import LISTING_GONE_STATUSES, ListingGone, ParseError
from price_tracker.core.health import HealthManager
from price_tracker.core.outlier import REQUIRED_CONFIRMATIONS

if TYPE_CHECKING:
    from price_tracker.core.alert import PriceAlert
    from price_tracker.core.registry import ScraperRegistry
    from price_tracker.db.models import ProductRecord
    from price_tracker.db.repository import Repository
    from price_tracker.observability.metrics import MetricsRegistry

CHECK_TICK_MINUTES = 5
MAX_INTERVAL_MINUTES = 7 * 24 * 60


def interval_floor_minutes(scraper: object) -> int:
    """Minimum minutes between two scheduled checks that ``scraper`` declares.

    ``0`` for ``None``, for an object without ``min_check_interval_minutes`` and for a
    value that is not a plain ``int`` (``bool`` included) or is negative; capped at
    ``MAX_INTERVAL_MINUTES``.
    """
    value = getattr(scraper, "min_check_interval_minutes", 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return min(value, MAX_INTERVAL_MINUTES)


def effective_interval_minutes(own: object, *, global_minutes: int, floor_minutes: int = 0) -> int:
    """The interval the scheduler applies to one product, in minutes.

    The product's own interval when it is a plain ``int`` in ``1..MAX_INTERVAL_MINUTES``,
    else ``global_minutes`` clamped to ``CHECK_TICK_MINUTES..MAX_INTERVAL_MINUTES``;
    never below ``floor_minutes``, which callers take from ``interval_floor_minutes``.
    """
    if isinstance(own, int) and not isinstance(own, bool) and 1 <= own <= MAX_INTERVAL_MINUTES:
        base = own
    else:
        base = min(max(global_minutes, CHECK_TICK_MINUTES), MAX_INTERVAL_MINUTES)
    return max(base, floor_minutes)


def configured_interval_minutes(saved: object, *, configured: int) -> int:
    """Read the stored global interval without unbounded integer conversion."""
    fallback = min(max(configured, CHECK_TICK_MINUTES), MAX_INTERVAL_MINUTES)
    if not isinstance(saved, str) or not saved or not saved.isascii() or not saved.isdigit():
        return fallback
    digits = saved.lstrip("0") or "0"
    cap = str(MAX_INTERVAL_MINUTES)
    if len(digits) > len(cap) or (len(digits) == len(cap) and digits > cap):
        return MAX_INTERVAL_MINUTES
    return max(CHECK_TICK_MINUTES, int(digits))


class ThumbnailRequester(Protocol):
    """Anything that can be asked, without waiting, to fetch a product image later."""

    def request(self, url: str | None, *, store: str | None = None) -> bool: ...


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


def floor_anchor(product: ProductRecord, *, attempted_at: object) -> datetime | None:
    """The latest readable moment the store was asked about ``product``.

    The newest of this process's own attempt, the stored read, the stored failure
    and the product's creation. A missing or unreadable value is skipped.
    """
    anchors: list[datetime] = []
    if isinstance(attempted_at, datetime) and attempted_at.utcoffset() is not None:
        anchors.append(attempted_at)
    for value in (product.last_checked_at, product.last_error_at, product.created_at):
        if not isinstance(value, str) or not value:
            continue
        try:
            anchors.append(_parse_db_timestamp(value))
        except (ValueError, TypeError, AttributeError, OverflowError):
            continue
    return max(anchors) if anchors else None


def floor_holds(
    product: ProductRecord, *, floor_minutes: int, attempted_at: object, now: datetime
) -> bool:
    """Whether a scheduled check of ``product`` must still wait for its scraper's floor.

    ``False`` without a floor and when no anchor is readable: the wait cannot be shown.
    """
    if floor_minutes <= 0:
        return False
    anchor = floor_anchor(product, attempted_at=attempted_at)
    return anchor is not None and now - anchor < timedelta(minutes=floor_minutes)


def _failure_reason(exc: BaseException) -> tuple[str, str]:
    """Map a failed check to a supported ``(reason, detail)`` pair."""
    if isinstance(exc, ListingGone):
        detail = f"HTTP {exc.status}" if exc.http_status_observed else "product page missing"
        return "listing_gone", detail
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
    domain_spacing: DomainSpacing = field(default_factory=lambda: DomainSpacing(min_gap=0))
    """Per-store spacing of scheduled checks; startup passes DOMAIN_MIN_GAP_SECONDS."""
    drop_floor: DropFloor = NO_DROP_FLOOR
    """Server-default minimum drop for an alert; startup passes MIN_DROP_PERCENT/ABSOLUTE."""
    thumbnails: ThumbnailRequester | None = None


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one product check in pull mode."""

    product_id: int
    user_id: int
    alert: PriceAlert | None = None
    disabled: bool = False
    reason: str | None = None
