"""Telegram notifier — sends alerts and chart embeds.

The notifier supports two dispatch paths:

- Legacy callable: ``await notifier(user_id, text)`` — used by the existing
  scheduler. Sends an HTML message to ``user_id`` and emits the
  ``notification_sent_total`` counter on success.
- Preferences-aware: ``await notifier.notify_alert(user_id=..., product_id=...,
  alert=...)`` — consults :class:`PreferencesManager` for mute / quiet-hours /
  throttle / digest, deduplicates by ``alert["event_id"]``, and either sends
  immediately, enqueues for digest delivery, or drops with a skipped-reason
  metric.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from price_tracker.core.textlimits import split_message
from price_tracker.db.models import NotificationPrefs
from price_tracker.notifier.preferences import (
    ThrottleWindow,
    is_muted_now,
    is_quiet_now,
)

if TYPE_CHECKING:
    from telegram import Bot

    from price_tracker.notifier.digest import DigestService
    from price_tracker.notifier.preferences import PreferencesManager
    from price_tracker.observability.metrics import MetricsRegistry

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(
        self,
        bot: Bot,
        *,
        metrics: MetricsRegistry | None = None,
        prefs: PreferencesManager | None = None,
        digest: DigestService | None = None,
    ) -> None:
        self._bot = bot
        self._metrics = metrics
        self._prefs = prefs
        self._digest = digest
        self._dedupe_seen: set[str] = set()  # event_id deduplication (in-process)

    async def __call__(
        self,
        user_id: int,
        text: str,
        *,
        product_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Deliver one message, returning whether the caller may consider it handled.

        With a ``product_id`` and a configured :class:`PreferencesManager` the
        message is routed through the user's preferences (mute, quiet hours,
        throttle, digest) — the same gates ``notify_alert`` applies. Without
        them it is sent directly: operational notices (auto-disable, quarantine)
        are not the kind of message a mute is meant to hide.

        ``True`` means the message reached the user or was queued for a digest
        that will. ``False`` means it did not and nothing else will deliver it —
        a send failure, or a preference that dropped it outright — so the caller
        must not record it as sent.
        """
        kind = (payload or {}).get("kind")
        if self._prefs is not None and (product_id is not None or kind == "operational"):
            alert: dict[str, Any] = {**(payload or {}), "text": text}
            return await self.notify_alert(user_id=user_id, product_id=product_id, alert=alert)
        return await self._send_direct(user_id, text, reply_markup=_markup(payload))

    async def _send_direct(
        self,
        user_id: int,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        chunks = split_message(text)
        for index, chunk in enumerate(chunks, start=1):
            try:
                await self._bot.send_message(
                    chat_id=user_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                    reply_markup=reply_markup if index == len(chunks) else None,
                )
            except Exception as e:  # noqa: BLE001 — Telegram errors are non-deterministic
                logger.warning(
                    "Telegram send failed for user %d at chunk %d/%d after %d delivered: %s",
                    user_id,
                    index,
                    len(chunks),
                    index - 1,
                    e,
                )
                return False
        if not chunks:
            return True
        if self._metrics is not None:
            self._metrics.notification_sent_total.labels(type="immediate", channel="telegram").inc()
        return True

    async def send_alert(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        """Send an HTML alert message and emit the immediate-sent metric."""
        chunks = split_message(text)
        for index, chunk in enumerate(chunks, start=1):
            await self._bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup if index == len(chunks) else None,
            )
        if not chunks:
            return
        if self._metrics is not None:
            self._metrics.notification_sent_total.labels(type="immediate", channel="telegram").inc()

    async def notify_alert(
        self, *, user_id: int, product_id: int | None, alert: dict[str, Any]
    ) -> bool:
        """Dispatch an alert respecting the user's effective preferences.

        Flow:
          1. Dedupe by ``alert["event_id"]`` (in-process).
          2. Resolve effective prefs.
          3. Mute → drop (``mute`` reason).
          4. Quiet hours → enqueue if digest_mode else drop.
          5. Throttle exceeded → enqueue if digest_mode else drop.
          6. Digest mode (no quiet/throttle gate) → enqueue.
          7. Otherwise immediate send.

        Returns ``True`` when the alert was sent or queued for a digest, and
        ``False`` when it was dropped. The distinction matters to the caller:
        a dropped alert must not advance cooldown bookkeeping, or the drop the
        user was waiting for is suppressed a second time once the preference
        that hid it no longer applies.
        """
        raw_event_id = alert.get("event_id")
        event_id = raw_event_id if isinstance(raw_event_id, str) else None
        if event_id is not None and event_id in self._dedupe_seen:
            return True  # already delivered once; not a delivery failure

        now = datetime.now(UTC)
        operational = alert.get("kind") == "operational"
        if self._prefs is None:
            eff = None
        elif operational:
            eff = await self._prefs.resolve_global(user_id=user_id)
        elif product_id is not None:
            eff = await self._prefs.resolve(user_id=user_id, product_id=product_id)
        else:
            logger.warning("Price alert for user %d has no product id", user_id)
            return False

        if eff is not None and not operational and is_muted_now(eff, now_utc=now):
            self._emit_skipped("mute")
            return False

        if eff is not None and is_quiet_now(eff, now_utc=now):
            if eff.digest_mode or operational:
                return await self._enqueue_for_digest(
                    user_id=user_id,
                    product_id=product_id,
                    alert=alert,
                    event_id=event_id,
                )
            self._emit_skipped("quiet_hours")
            return False

        if eff is not None and eff.throttle_per_hour is not None and self._prefs is not None:
            # Load throttle window from prefs row (fetch fresh)
            row = await self._prefs._repo.get_notification_prefs(  # noqa: SLF001
                user_id=user_id, product_id=None
            )
            window = ThrottleWindow.from_json(row.throttle_state_json if row else None)
            if window.exceeded(limit=eff.throttle_per_hour, now=now):
                if eff.digest_mode or operational:
                    return await self._enqueue_for_digest(
                        user_id=user_id,
                        product_id=product_id,
                        alert=alert,
                        event_id=event_id,
                    )
                self._emit_skipped("throttle")
                return False
            window.record(now)
            if row is not None:
                updated = dataclasses.replace(row, throttle_state_json=window.to_json())
            else:
                updated = NotificationPrefs(
                    user_id=user_id,
                    product_id=None,
                    throttle_state_json=window.to_json(),
                )
            await self._prefs._repo.upsert_notification_prefs(updated)  # noqa: SLF001

        if eff is not None and eff.digest_mode and self._digest is not None:
            return await self._enqueue_for_digest(
                user_id=user_id,
                product_id=product_id,
                alert=alert,
                event_id=event_id,
            )

        # A caller that already rendered the message (the scheduler's rich
        # price-drop body) keeps its text; the dict-only fallback is for callers
        # that hand over structured data alone.
        text = alert.get("text") or _format_alert_message(alert)
        await self.send_alert(chat_id=user_id, text=text, reply_markup=_markup(alert))
        self._mark_delivered(event_id)
        return True

    async def _enqueue_for_digest(
        self,
        *,
        user_id: int,
        product_id: int | None,
        alert: dict[str, Any],
        event_id: str | None,
    ) -> bool:
        """Queue a deferred notification, keeping an unqueueable event visible."""
        if self._digest is None:
            logger.warning("Cannot defer notification for user %d: digest is unavailable", user_id)
            return False
        # The queue schema accepts a NULL owner for domain-level operational notices.
        # DigestService's public annotation is widened by its owning task.
        await self._digest.enqueue(
            user_id=user_id,
            product_id=cast("int", product_id),
            payload=alert,
        )
        self._emit_skipped("digest_pending")
        self._mark_delivered(event_id)
        return True

    def _mark_delivered(self, event_id: str | None) -> None:
        """Remember an event only once it was sent or queued.

        Recording it on arrival instead poisoned every retry of an alert that a
        preference had dropped: the retry hit the dedupe set and reported
        success for a message nobody ever delivered.
        """
        if event_id is not None:
            self._dedupe_seen.add(event_id)

    def _emit_skipped(self, reason: str) -> None:
        if self._metrics is not None:
            self._metrics.notification_skipped_total.labels(reason=reason).inc()


def _format_alert_message(alert: dict[str, Any]) -> str:
    """Compose the immediate-send HTML alert body (English)."""
    name = alert.get("product_name", "Product")
    old = alert.get("old_price", "?")
    new = alert.get("new_price", "?")
    currency = alert.get("currency", "")
    domain = alert.get("domain", "")
    try:
        arrow = "🔻" if float(new) < float(old) else "🔺"
    except (TypeError, ValueError):
        arrow = "•"
    return f"{arrow} <b>{name}</b>\n{currency}{old} → {currency}{new}\n{domain}"


def _markup(payload: dict[str, Any] | None) -> InlineKeyboardMarkup | None:
    """Build a safe inline keyboard from a serializable operational payload."""
    if payload is None or "buttons" not in payload:
        return None
    buttons = payload["buttons"]
    if not isinstance(buttons, list):
        logger.warning("Ignoring malformed notification buttons")
        return None

    rows: list[list[InlineKeyboardButton]] = []
    for row in buttons:
        if not isinstance(row, list):
            logger.warning("Ignoring malformed notification buttons")
            return None
        markup_row: list[InlineKeyboardButton] = []
        for button in row:
            if not isinstance(button, dict):
                logger.warning("Ignoring malformed notification buttons")
                return None
            text = button.get("text")
            callback_data = button.get("callback_data")
            if (
                not isinstance(text, str)
                or not isinstance(callback_data, str)
                or len(callback_data.encode("utf-8")) > 64
            ):
                logger.warning("Ignoring malformed notification buttons")
                return None
            markup_row.append(InlineKeyboardButton(text=text, callback_data=callback_data))
        rows.append(markup_row)
    return InlineKeyboardMarkup(rows)
