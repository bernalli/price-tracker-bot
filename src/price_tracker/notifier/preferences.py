"""Per-user/per-product notification preferences with resolution chain.

Resolution order: per-product → per-user-global → defaults.
NULL fields fall through.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import TYPE_CHECKING, TypeVar
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from price_tracker.db.repository import Repository

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class EffectivePrefs:
    """Resolved per-user/per-product notification preferences.

    Each field is the result of coalescing per-product → per-user-global → default.
    """

    mute: bool
    mute_until: datetime | None
    digest_mode: bool
    digest_interval_minutes: int
    quiet_hours_start: str | None
    quiet_hours_end: str | None
    throttle_per_hour: int | None
    timezone: str


_DEFAULTS = EffectivePrefs(
    mute=False,
    mute_until=None,
    digest_mode=False,
    digest_interval_minutes=60,
    quiet_hours_start=None,
    quiet_hours_end=None,
    throttle_per_hour=None,
    timezone="Europe/Rome",
)


def _coalesce(*values: T | None) -> T | None:
    """Return the first non-None value, or None if all are None."""
    for v in values:
        if v is not None:
            return v
    return None


class PreferencesManager:
    """Resolve per-user/per-product notification preferences.

    Resolution chain (field-by-field): per-product row → per-user-global row → defaults.
    NULL fields fall through to the next layer.
    """

    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    async def resolve(self, *, user_id: int, product_id: int) -> EffectivePrefs:
        """Return the effective preferences for ``(user_id, product_id)``."""
        per_product = await self._repo.get_notification_prefs(
            user_id=user_id, product_id=product_id
        )
        per_user = await self._repo.get_notification_prefs(user_id=user_id, product_id=None)

        def _pick_bool(name: str, default: bool) -> bool:
            pp = getattr(per_product, name, None) if per_product is not None else None
            pu = getattr(per_user, name, None) if per_user is not None else None
            v = _coalesce(pp, pu)
            return bool(v) if v is not None else default

        def _pick_int(name: str, default: int) -> int:
            pp = getattr(per_product, name, None) if per_product is not None else None
            pu = getattr(per_user, name, None) if per_user is not None else None
            v = _coalesce(pp, pu)
            return int(v) if v is not None else default

        def _pick_str(name: str, default: str) -> str:
            pp = getattr(per_product, name, None) if per_product is not None else None
            pu = getattr(per_user, name, None) if per_user is not None else None
            v = _coalesce(pp, pu)
            return str(v) if v is not None else default

        def _pick_optional_str(name: str) -> str | None:
            pp = getattr(per_product, name, None) if per_product is not None else None
            pu = getattr(per_user, name, None) if per_user is not None else None
            v = _coalesce(pp, pu)
            return str(v) if v is not None else None

        def _pick_optional_int(name: str) -> int | None:
            pp = getattr(per_product, name, None) if per_product is not None else None
            pu = getattr(per_user, name, None) if per_user is not None else None
            v = _coalesce(pp, pu)
            return int(v) if v is not None else None

        def _pick_optional_dt(name: str) -> datetime | None:
            pp = getattr(per_product, name, None) if per_product is not None else None
            pu = getattr(per_user, name, None) if per_user is not None else None
            v = _coalesce(pp, pu)
            if v is None:
                return None
            assert isinstance(v, datetime)
            return v

        return EffectivePrefs(
            mute=_pick_bool("mute", _DEFAULTS.mute),
            mute_until=_pick_optional_dt("mute_until"),
            digest_mode=_pick_bool("digest_mode", _DEFAULTS.digest_mode),
            digest_interval_minutes=_pick_int(
                "digest_interval_minutes", _DEFAULTS.digest_interval_minutes
            ),
            quiet_hours_start=_pick_optional_str("quiet_hours_start"),
            quiet_hours_end=_pick_optional_str("quiet_hours_end"),
            throttle_per_hour=_pick_optional_int("throttle_per_hour"),
            timezone=_pick_str("timezone", _DEFAULTS.timezone),
        )


def _parse_hhmm(hhmm: str) -> time:
    """Parse a ``"HH:MM"`` string into a :class:`datetime.time`."""
    h, _, m = hhmm.partition(":")
    return time(int(h), int(m))


def is_quiet_now(prefs: EffectivePrefs, *, now_utc: datetime) -> bool:
    """Return True if ``now_utc`` falls inside the user's quiet-hours window.

    The window is defined in the user's local timezone (``prefs.timezone``) and
    may wrap midnight (e.g. ``22:00`` → ``08:00``). If either bound is missing
    the function returns False (no window configured).
    """
    if not prefs.quiet_hours_start or not prefs.quiet_hours_end:
        return False
    user_now = now_utc.astimezone(ZoneInfo(prefs.timezone))
    start = _parse_hhmm(prefs.quiet_hours_start)
    end = _parse_hhmm(prefs.quiet_hours_end)
    cur = user_now.time()
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end  # wraps midnight


def is_muted_now(prefs: EffectivePrefs, *, now_utc: datetime) -> bool:
    """Return True if mute is currently active.

    Mute can be permanent (``mute_until is None``) or expire at a given UTC
    instant. If ``mute`` is False, mute is never active.
    """
    if not prefs.mute:
        return False
    if prefs.mute_until is None:
        return True
    return now_utc < prefs.mute_until
