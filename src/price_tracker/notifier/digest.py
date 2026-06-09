"""Digest queue + flush logic for batched notifications (Feature D).

Three flush triggers:
  1. Interval — every digest_interval_minutes per user with pending entries
  2. Quiet-hours-end — flush at the moment a user's quiet window ends
  3. Manual — /digest_now command
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from telegram import Bot

    from price_tracker.db.models import DigestEntry
    from price_tracker.db.repository import Repository
    from price_tracker.observability.metrics import MetricsRegistry

log = logging.getLogger(__name__)


def _format_digest_message(entries: list[DigestEntry]) -> str:
    """Compose the HTML digest message body (English)."""
    parts = [
        f"📊 <b>Digest — {len(entries)} price change{'s' if len(entries) != 1 else ''}</b>",
        "",
    ]
    for e in entries:
        try:
            data = json.loads(e.alert_payload_json)
        except json.JSONDecodeError:
            continue
        name = data.get("product_name", f"Product #{e.product_id}")
        old = data.get("old_price", "?")
        new = data.get("new_price", "?")
        currency = data.get("currency", "")
        domain = data.get("domain", "")
        try:
            arrow = "🔻" if float(new) < float(old) else "🔺"
        except (TypeError, ValueError):
            arrow = "•"
        parts.append(f"{arrow} {name} — {currency}{old} → {currency}{new} — {domain}")
    parts.append("")
    parts.append("Use /lista for full state.")
    return "\n".join(parts)


class DigestService:
    """Manage digest enqueue and flush operations."""

    def __init__(
        self,
        *,
        repo: Repository,
        bot: Bot,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._repo = repo
        self._bot = bot
        self._metrics = metrics

    async def enqueue(self, *, user_id: int, product_id: int, payload: dict[str, Any]) -> int:
        """Enqueue an alert payload for digest delivery."""
        eid = await self._repo.enqueue_digest(
            user_id=user_id, product_id=product_id, payload=json.dumps(payload)
        )
        if self._metrics is not None:
            self._metrics.notification_skipped_total.labels(reason="digest_pending").inc()
        return eid

    async def flush_user(self, *, user_id: int) -> int:
        """Flush all pending digest entries for a single user. Returns count flushed."""
        entries = await self._repo.list_pending_digest(user_id=user_id)
        if not entries:
            return 0
        text = _format_digest_message(entries)
        await self._bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
        await self._repo.mark_digest_flushed([e.id for e in entries if e.id is not None])
        if self._metrics is not None:
            self._metrics.notification_sent_total.labels(type="digest", channel="telegram").inc()
        return len(entries)

    async def flush_due(self, *, interval_minutes: int) -> int:
        """Flush each user whose oldest pending entry exceeds their digest interval.

        Per-user ``digest_interval_minutes`` is honoured; ``interval_minutes`` is the
        fallback when a user has no stored preference.
        """
        flushed_total = 0
        users = await self._repo.list_users_with_pending_digest()
        now = datetime.now(UTC)
        for user_id, oldest_enqueued_at in users:
            prefs = await self._repo.get_notification_prefs(user_id=user_id, product_id=None)
            threshold = (
                prefs.digest_interval_minutes
                if prefs is not None and prefs.digest_interval_minutes
                else interval_minutes
            )
            age = (now - oldest_enqueued_at).total_seconds() / 60.0
            if age >= threshold:
                flushed_total += await self.flush_user(user_id=user_id)
        return flushed_total
