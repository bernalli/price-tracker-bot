"""/intervallo must not crash on the frozen Config and must take effect (#14/#15).

cmd_set_interval did `config.check_interval_minutes = minutes` on a
@dataclass(frozen=True) Config → FrozenInstanceError, so the interval was never
persisted nor applied. Fix: persist to DB and reschedule the live job.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from price_tracker.bot.handlers.settings import cmd_set_interval
from price_tracker.config import Config


def _make_config() -> Config:
    return Config(
        telegram_bot_token="x",
        admin_users=(),
        check_interval_minutes=360,
        database_path=":memory:",
        default_threshold_type="percentage",
        default_threshold_value="10",
        max_consecutive_errors=10,
        check_delay_seconds=0.0,
        notification_cooldown_hours=24,
        request_timeout=30,
        log_level="INFO",
        lang="it",
    )


async def test_set_interval_persists_and_reschedules_without_crashing() -> None:
    db = AsyncMock()
    db.is_user_admin = AsyncMock(return_value=True)
    job_queue = MagicMock()
    job_queue.get_jobs_by_name.return_value = []
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_user.language_code = "it"
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["120"]
    context.bot_data = {"db": db, "config": _make_config()}
    context.job_queue = job_queue

    await cmd_set_interval(update, context)  # must NOT raise FrozenInstanceError

    db.set_config.assert_awaited_once_with("check_interval_minutes", "120")
    job_queue.run_repeating.assert_called_once()
    # rescheduled at the requested cadence (seconds)
    _, kwargs = job_queue.run_repeating.call_args
    assert kwargs.get("interval") == 120 * 60
