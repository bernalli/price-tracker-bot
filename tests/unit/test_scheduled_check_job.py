"""The periodic job reads the global interval on every tick and never goes below the tick."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.main import scheduled_check_job


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        (None, 360),
        ("", 360),
        ("90", 90),
        ("0", 5),
        ("3", 5),
        ("-60", 360),
        ("abc", 360),
        ("1.5", 360),
    ],
)
async def test_the_job_passes_the_stored_global_interval(saved: str | None, expected: int) -> None:
    scheduler = MagicMock()
    scheduler.deps.repo.get_config = AsyncMock(return_value=saved)
    scheduler.run_check_due = AsyncMock()
    context = MagicMock()
    context.application.bot_data = {
        "scheduler": scheduler,
        "config": SimpleNamespace(check_interval_minutes=360),
    }

    await scheduled_check_job(context)

    scheduler.run_check_due.assert_awaited_once_with(global_interval_minutes=expected)
