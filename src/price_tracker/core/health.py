# src/price_tracker/core/health.py
"""Per-domain quarantine state machine (Feature B).

Tracks consecutive block events per eTLD+1, transitions through
CLOSED → LOCKED_T1 → HALF_OPEN_T1 → CLOSED|LOCKED_T2 → ... → LOCKED_T3 (sentinel).
"""

from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from price_tracker.db.models import ScraperHealth

if TYPE_CHECKING:
    from price_tracker.db.repository import Repository
    from price_tracker.observability.metrics import MetricsRegistry


class QuarantineState(enum.StrEnum):
    CLOSED = "CLOSED"
    LOCKED_T1 = "LOCKED_T1"
    LOCKED_T2 = "LOCKED_T2"
    LOCKED_T3 = "LOCKED_T3"
    HALF_OPEN_T1 = "HALF_OPEN_T1"
    HALF_OPEN_T2 = "HALF_OPEN_T2"
    HALF_OPEN_T3 = "HALF_OPEN_T3"


@dataclass(frozen=True, slots=True)
class _TierConfig:
    threshold: int
    lockout: timedelta
    locked: QuarantineState
    half_open: QuarantineState


_TIERS: tuple[_TierConfig, ...] = (
    _TierConfig(3, timedelta(hours=1), QuarantineState.LOCKED_T1, QuarantineState.HALF_OPEN_T1),
    _TierConfig(6, timedelta(hours=6), QuarantineState.LOCKED_T2, QuarantineState.HALF_OPEN_T2),
    _TierConfig(12, timedelta(hours=24), QuarantineState.LOCKED_T3, QuarantineState.HALF_OPEN_T3),
)


def _now_utc() -> datetime:
    return datetime.now(UTC)


class HealthManager:
    """In-memory health state with write-through persistence to the Repository."""

    def __init__(
        self,
        repo: Repository,
        *,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._repo = repo
        self._records: dict[str, ScraperHealth] = {}
        self._lock = asyncio.Lock()
        self._metrics = metrics

    async def load(self) -> None:
        rows = await self._repo.list_all_scraper_health()
        self._records = {row.domain: row for row in rows}

    def state(self, domain: str) -> QuarantineState:
        record = self._records.get(domain)
        if record is None:
            return QuarantineState.CLOSED
        st = QuarantineState(record.state)
        if (
            st in (QuarantineState.LOCKED_T1, QuarantineState.LOCKED_T2, QuarantineState.LOCKED_T3)
            and record.locked_until
            and _now_utc() >= record.locked_until
        ):
            return _LOCKED_TO_HALF_OPEN[st]
        return st

    def is_locked(self, domain: str) -> bool:
        st = self.state(domain)
        return st in (
            QuarantineState.LOCKED_T1,
            QuarantineState.LOCKED_T2,
            QuarantineState.LOCKED_T3,
        )

    def is_half_open(self, domain: str) -> bool:
        st = self.state(domain)
        return st in (
            QuarantineState.HALF_OPEN_T1,
            QuarantineState.HALF_OPEN_T2,
            QuarantineState.HALF_OPEN_T3,
        )

    def locked_until(self, domain: str) -> datetime | None:
        record = self._records.get(domain)
        return record.locked_until if record else None

    def consecutive_blocks(self, domain: str) -> int:
        record = self._records.get(domain)
        return record.consecutive_blocks if record else 0

    def all_records(self) -> list[ScraperHealth]:
        return list(self._records.values())

    async def record_block(self, domain: str, *, reason: str) -> QuarantineState:
        async with self._lock:
            now = _now_utc()
            current = self._records.get(domain)
            base_count = current.consecutive_blocks if current else 0
            new_count = base_count + 1

            current_state = self.state(domain)
            new_state: QuarantineState
            locked_until: datetime | None
            if current_state in (
                QuarantineState.HALF_OPEN_T1,
                QuarantineState.HALF_OPEN_T2,
                QuarantineState.HALF_OPEN_T3,
            ):
                # half-open block → promote to next tier
                next_tier = _HALF_OPEN_TO_NEXT_TIER.get(current_state)
                if next_tier is None:
                    # already at T3 sentinel — stay LOCKED_T3, refresh lockout
                    new_state = QuarantineState.LOCKED_T3
                    locked_until = now + timedelta(hours=24)
                    new_count = max(new_count, 12)
                else:
                    new_state = next_tier.locked
                    locked_until = now + next_tier.lockout
                    new_count = max(new_count, next_tier.threshold)
            else:
                new_state = QuarantineState.CLOSED
                locked_until = None
                for tier in reversed(_TIERS):
                    if new_count >= tier.threshold:
                        new_state = tier.locked
                        locked_until = now + tier.lockout
                        break

            updated = ScraperHealth(
                domain=domain,
                state=new_state.value,
                consecutive_blocks=new_count,
                locked_until=locked_until,
                last_block_at=now,
                last_block_reason=reason,
                last_success_at=current.last_success_at if current else None,
            )
            self._records[domain] = updated
            await self._repo.upsert_scraper_health(updated)
            if self._metrics is not None:
                if current_state != new_state:
                    self._metrics.quarantine_transitions_total.labels(
                        domain=domain,
                        from_state=current_state.value,
                        to_state=new_state.value,
                    ).inc()
                self._metrics.quarantine_state.labels(domain=domain).set(_state_to_int(new_state))
            return new_state

    async def record_success(self, domain: str) -> QuarantineState:
        async with self._lock:
            now = _now_utc()
            current = self._records.get(domain)
            current_state = self.state(domain)
            updated = ScraperHealth(
                domain=domain,
                state=QuarantineState.CLOSED.value,
                consecutive_blocks=0,
                locked_until=None,
                last_block_at=current.last_block_at if current else None,
                last_block_reason=current.last_block_reason if current else None,
                last_success_at=now,
            )
            self._records[domain] = updated
            await self._repo.upsert_scraper_health(updated)
            if self._metrics is not None:
                if current_state != QuarantineState.CLOSED:
                    self._metrics.quarantine_transitions_total.labels(
                        domain=domain,
                        from_state=current_state.value,
                        to_state=QuarantineState.CLOSED.value,
                    ).inc()
                self._metrics.quarantine_state.labels(domain=domain).set(
                    _state_to_int(QuarantineState.CLOSED)
                )
            return QuarantineState.CLOSED


_LOCKED_TO_HALF_OPEN: dict[QuarantineState, QuarantineState] = {
    QuarantineState.LOCKED_T1: QuarantineState.HALF_OPEN_T1,
    QuarantineState.LOCKED_T2: QuarantineState.HALF_OPEN_T2,
    QuarantineState.LOCKED_T3: QuarantineState.HALF_OPEN_T3,
}

_HALF_OPEN_TO_NEXT_TIER: dict[QuarantineState, _TierConfig | None] = {
    QuarantineState.HALF_OPEN_T1: _TIERS[1],
    QuarantineState.HALF_OPEN_T2: _TIERS[2],
    QuarantineState.HALF_OPEN_T3: None,  # sentinel — stay LOCKED_T3
}


_STATE_TO_INT: dict[QuarantineState, int] = {
    QuarantineState.CLOSED: 0,
    QuarantineState.LOCKED_T1: 1,
    QuarantineState.LOCKED_T2: 2,
    QuarantineState.LOCKED_T3: 3,
    QuarantineState.HALF_OPEN_T1: 4,
    QuarantineState.HALF_OPEN_T2: 4,
    QuarantineState.HALF_OPEN_T3: 4,
}


def _state_to_int(state: QuarantineState) -> int:
    """Map QuarantineState to numeric gauge value (0=CLOSED 1=T1 2=T2 3=T3 4=HALF_OPEN)."""
    return _STATE_TO_INT[state]
