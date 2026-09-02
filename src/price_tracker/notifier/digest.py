"""Digest queue + flush logic for batched notifications (Feature D).

Three flush triggers:
  1. Interval — every digest_interval_minutes per user with pending entries
  2. Quiet hours — users inside their quiet window are skipped; their entries
     flush at the first tick after the window ends
  3. Manual — /digest_now command
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, Any

from price_tracker.bot.messages import _, ngettext, reset_locale, set_locale
from price_tracker.core.alert import _why
from price_tracker.core.textlimits import DOMAIN_BUDGET, NAME_BUDGET, paginate, truncate_visible
from price_tracker.notifier.preferences import EffectivePrefs, is_quiet_now

if TYPE_CHECKING:
    from telegram import Bot

    from price_tracker.db.models import DigestEntry
    from price_tracker.db.repository import Repository
    from price_tracker.observability.metrics import MetricsRegistry

log = logging.getLogger(__name__)


def _safe_text(value: object, *, fallback: str, budget: int | None = None) -> str:
    """Return an escaped, optionally bounded string for a digest row."""
    text = str(value) if value is not None else fallback
    if budget is not None:
        text = truncate_visible(text, budget)
    return escape(text)


def _positive_int(value: object, *, fallback: int) -> int:
    """Return a positive integer payload value, or a conservative fallback."""
    if not isinstance(value, (int, float, str)):
        return fallback
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _price_block(entry: DigestEntry, payload: dict[str, Any]) -> str:
    """Render one conventional price-change digest row."""
    fallback_name = (
        _("Product #{product_id}").format(product_id=entry.product_id)
        if entry.product_id is not None
        else _("Operational notice")
    )
    name = _safe_text(payload.get("product_name"), fallback=fallback_name, budget=NAME_BUDGET)
    old = _safe_text(payload.get("old_price"), fallback="?")
    new = _safe_text(payload.get("new_price"), fallback="?")
    currency = _safe_text(payload.get("currency"), fallback="")
    domain = _safe_text(payload.get("domain"), fallback="", budget=DOMAIN_BUDGET)
    old_value = payload.get("old_price")
    new_value = payload.get("new_price")
    try:
        if not isinstance(old_value, (int, float, str)) or not isinstance(
            new_value, (int, float, str)
        ):
            raise ValueError
        arrow = "🔻" if float(new_value) < float(old_value) else "🔺"
    except (TypeError, ValueError):
        arrow = "•"
    return f"{arrow} {name} — {currency}{old} → {currency}{new} — {domain}"


def _operational_block(payload: dict[str, Any], *, include_heading: bool) -> str:
    """Render one operational digest row, using safe defaults for sparse payloads."""
    domain = _safe_text(payload.get("domain"), fallback=_("unknown"), budget=DOMAIN_BUDGET)
    event = payload.get("event")
    if event == "warning":
        count = _positive_int(payload.get("count"), fallback=1)
        maximum = _positive_int(payload.get("max"), fallback=1)
        row = _("{domain} — {n} products: checks failing ({count}/{max})").format(
            domain=domain, n=count, count=count, max=maximum
        )
    elif event == "quarantine":
        row = _("{domain} — quarantined").format(domain=domain)
    else:
        count = _positive_int(payload.get("count"), fallback=1)
        reason = payload.get("reason")
        why = _why(reason if isinstance(reason, str) else None, None)
        row = _("{domain} — {n} products: tracking suspended ({why})").format(
            domain=domain, n=count, why=escape(why)
        )
    heading = _("⚠️ Operational notices")
    return f"{heading}\n{row}" if include_heading else row


def _digest_blocks(entries: list[DigestEntry]) -> tuple[str, list[tuple[int, str]], str, list[int]]:
    """Build digest pages inputs and collect JSON rows that cannot be rendered."""
    price_blocks: list[tuple[int, str]] = []
    operational_blocks: list[tuple[int, str]] = []
    unrenderable_ids: list[int] = []
    for entry in entries:
        try:
            payload = json.loads(entry.alert_payload_json)
        except (TypeError, json.JSONDecodeError):
            if entry.id is not None:
                unrenderable_ids.append(entry.id)
            continue
        if not isinstance(payload, dict):
            if entry.id is not None:
                unrenderable_ids.append(entry.id)
            continue
        if entry.id is None:
            continue
        if payload.get("kind") == "operational":
            operational_blocks.append(
                (entry.id, _operational_block(payload, include_heading=not operational_blocks))
            )
        else:
            price_blocks.append((entry.id, _price_block(entry, payload)))

    price_count = len(price_blocks)
    header = ngettext(
        "📊 <b>Digest — {n} price change</b>",
        "📊 <b>Digest — {n} price changes</b>",
        price_count,
    ).format(n=price_count)
    footer_parts = [_("Use /lista for full state.")]
    if operational_blocks:
        footer_parts.insert(0, _("Use /reactivate or /errori for details."))
    return header, [*price_blocks, *operational_blocks], "\n".join(footer_parts), unrenderable_ids


def _row_is_quiet(prefs: object, *, now: datetime) -> bool:
    """Evaluate quiet hours from the user's global preference row only."""
    quiet_start = getattr(prefs, "quiet_hours_start", None)
    quiet_end = getattr(prefs, "quiet_hours_end", None)
    timezone = getattr(prefs, "timezone", None)
    if (
        not isinstance(quiet_start, str)
        or not isinstance(quiet_end, str)
        or not isinstance(timezone, str)
    ):
        return False
    effective = EffectivePrefs(
        mute=False,
        mute_until=None,
        digest_mode=False,
        digest_interval_minutes=0,
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
        throttle_per_hour=None,
        timezone=timezone,
    )
    return is_quiet_now(effective, now_utc=now)


class DigestService:
    """Manage digest enqueue and flush operations."""

    def __init__(
        self,
        *,
        repo: Repository,
        bot: Bot,
        metrics: MetricsRegistry | None = None,
        lang: str | None = None,
    ) -> None:
        self._repo = repo
        self._bot = bot
        self._metrics = metrics
        self._lang = lang

    async def enqueue(
        self, *, user_id: int, product_id: int | None, payload: dict[str, Any]
    ) -> int:
        """Enqueue an alert payload for digest delivery.

        The ``digest_pending`` skip metric is emitted by the caller
        (TelegramNotifier) — not here — so one routed alert counts once.
        """
        return await self._repo.enqueue_digest(
            user_id=user_id, product_id=product_id, payload=json.dumps(payload)
        )

    async def flush_user(self, *, user_id: int) -> int:
        """Flush all pending digest entries for a single user. Returns count flushed."""
        entries = await self._repo.list_pending_digest(user_id=user_id)
        if not entries:
            return 0
        header, blocks, footer, unrenderable_ids = _digest_blocks(entries)
        pages = paginate(header, blocks, footer)
        flushed_count = 0
        unrenderable_pending = unrenderable_ids.copy()
        for text, page_ids in pages:
            await self._bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
            ids_to_flush = [*page_ids, *unrenderable_pending]
            for entry_id in unrenderable_pending:
                log.warning(
                    "Quarantining unreadable digest entry_id=%s user_id=%s", entry_id, user_id
                )
            unrenderable_pending.clear()
            await self._repo.mark_digest_flushed(ids_to_flush)
            flushed_count += len(ids_to_flush)
            if self._metrics is not None:
                self._metrics.notification_sent_total.labels(
                    type="digest", channel="telegram"
                ).inc()
        if unrenderable_pending:
            for entry_id in unrenderable_pending:
                log.warning(
                    "Quarantining unreadable digest entry_id=%s user_id=%s", entry_id, user_id
                )
            await self._repo.mark_digest_flushed(unrenderable_pending)
            flushed_count += len(unrenderable_pending)
        return flushed_count

    async def flush_due(self, *, interval_minutes: int) -> int:
        """Flush each user whose oldest pending entry exceeds their digest interval.

        Per-user ``digest_interval_minutes`` is honoured; ``interval_minutes`` is the
        fallback when a user has no stored preference.
        """
        token = set_locale(self._lang)
        try:
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
                if _row_is_quiet(prefs, now=now) if prefs is not None else False:
                    continue
                if age >= threshold:
                    flushed_total += await self.flush_user(user_id=user_id)
            return flushed_total
        finally:
            reset_locale(token)
