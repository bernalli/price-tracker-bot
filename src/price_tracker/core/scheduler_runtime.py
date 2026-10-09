"""Late-bound calls that preserve the public scheduler monkeypatch seam."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from price_tracker.core.health import HealthManager
    from price_tracker.core.notices import NoticeGroup


def format_operational_notice(group: NoticeGroup) -> str:
    """Resolve the public facade attribute at call time."""
    from price_tracker.core import scheduler  # noqa: PLC0415

    return scheduler.format_operational_notice(group)


async def handle_success_in_pipeline(*, health_mgr: HealthManager, domain: str) -> None:
    """Resolve the public facade attribute at call time."""
    from price_tracker.core import scheduler  # noqa: PLC0415

    await scheduler.handle_success_in_pipeline(health_mgr=health_mgr, domain=domain)
