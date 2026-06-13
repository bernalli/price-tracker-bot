"""admin_interval text input must enforce the 7-day upper bound (#63).

The inline admin-interval flow (`handle_text_input`, branch `admin_interval`)
validated only `minutes < 5`: an input like "9999999" was persisted via
`set_config` and confirmed ("ogni 166666.6 ore"), while the `/intervallo`
command path (settings.py) and monitoring both cap at 1440 * 7 minutes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from price_tracker.bot.handlers.text_input import handle_text_input
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


async def test_admin_interval_over_seven_days_is_rejected() -> None:
    db = AsyncMock()
    db.is_user_allowed = AsyncMock(return_value=True)
    db.is_user_admin = AsyncMock(return_value=True)
    db.get_product = AsyncMock(return_value={"name": "x"})
    job_queue = MagicMock()
    job_queue.get_jobs_by_name.return_value = []
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_user.language_code = "it"
    update.message.text = "9999999"
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {"pending_action": ("admin_interval", 0)}
    context.bot_data = {"db": db, "config": _make_config()}
    context.job_queue = job_queue

    await handle_text_input(update, context)

    db.set_config.assert_not_awaited()
    job_queue.run_repeating.assert_not_called()
    msg = update.message.reply_text.await_args.args[0]
    assert "massimo" in msg
    assert "7 giorni" in msg
    # The pending action must survive so the admin can retry with a valid value.
    assert context.user_data["pending_action"] == ("admin_interval", 0)
