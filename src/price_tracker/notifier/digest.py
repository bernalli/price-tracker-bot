"""Digest queue + flush logic for batched notifications.

Three flush triggers:
  1. Interval — every digest_interval_minutes per user with pending entries
  2. Quiet hours — users inside their quiet window are skipped; their entries
     flush at the first tick after the window ends
  3. Manual — /digest_now command
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, Any

from price_tracker.bot.messages import (
    _,
    current_locale,
    ngettext,
    reset_locale,
    set_locale,
    user_locale,
)
from price_tracker.bot.ui.product_rows import amount, product_row
from price_tracker.core.alert import _why
from price_tracker.core.notices import MAX_LISTED_PRODUCTS
from price_tracker.core.textlimits import DOMAIN_BUDGET, NAME_BUDGET, paginate, truncate_visible
from price_tracker.i18n.format import money
from price_tracker.notifier.preferences import (
    EffectivePrefs,
    PreferencesManager,
    is_muted_now,
    is_quiet_now,
)

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


def _optional_positive_int(value: object) -> int | None:
    """Return a strictly positive integral payload value, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _positive_int(value: object, *, fallback: int) -> int:
    """Return a positive integer payload value, or a conservative fallback."""
    return _optional_positive_int(value) or fallback


def _price_block(entry: DigestEntry, payload: dict[str, Any]) -> str:
    """Render one conventional price-change digest row."""
    fallback_name = (
        _("Product #{product_id}").format(product_id=entry.product_id)
        if entry.product_id is not None
        else _("Operational notice")
    )
    return product_row(
        str(payload.get("product_name") or fallback_name),
        amount(payload.get("new_price")),
        initial=amount(payload.get("old_price")),
        currency=str(payload.get("currency") or "EUR"),
    )


def _operational_block(payload: dict[str, Any], *, include_heading: bool) -> str:
    """Render one operational digest row, using safe defaults for sparse payloads."""
    domain = _safe_text(payload.get("domain"), fallback=_("unknown"), budget=DOMAIN_BUDGET)
    event = payload.get("event")
    if payload.get("historical") is True:
        row = _("Previous notice for {domain}: use /lista for the current status.").format(
            domain=domain
        )
    elif event == "warning":
        products = _positive_int(payload.get("count"), fallback=1)
        errors = _optional_positive_int(payload.get("error_count"))
        maximum = _optional_positive_int(payload.get("max_errors"))
        if errors is None or maximum is None or errors > maximum:
            errors_display: int | str = "?"
            maximum_display: int | str = "?"
        else:
            errors_display = errors
            maximum_display = maximum
        row = ngettext(
            "{domain} — {n} product: checks failing",
            "{domain} — {n} products: checks failing",
            products,
        ).format(
            domain=domain,
            n=products,
        )
        if not payload.get("products"):
            row += f" ({errors_display}/{maximum_display})"
    elif event == "quarantine":
        row = _("{domain} — quarantined").format(domain=domain)
    elif event in {"unavailable", "restocked"}:
        name = _safe_text(payload.get("product_name"), fallback=_("Product"), budget=NAME_BUDGET)
        row = (
            _("{name} — currently unavailable; monitoring continues")
            if event == "unavailable"
            else _("{name} — available again")
        ).format(name=name)
        price = amount(payload.get("last_price"))
        if event == "restocked" and price is not None:
            row += "\n" + _("Last recorded price: {price}").format(
                price=money(price, str(payload.get("currency") or "EUR"), locale=current_locale())
            )
    else:
        count = _positive_int(payload.get("count"), fallback=1)
        reason = payload.get("reason")
        why = _why(reason if isinstance(reason, str) else None, None)
        row = ngettext(
            "{domain} — {n} product: tracking suspended ({why})",
            "{domain} — {n} products: tracking suspended ({why})",
            count,
        ).format(domain=domain, n=count, why=escape(why))
    product_rows = payload.get("products")
    if isinstance(product_rows, list):
        details = []
        for product in product_rows[:MAX_LISTED_PRODUCTS]:
            if not isinstance(product, dict):
                continue
            pid = _optional_positive_int(product.get("id"))
            if pid is None:
                continue
            name = _safe_text(product.get("name"), fallback=_("Product"), budget=NAME_BUDGET)
            reason = product.get("reason", payload.get("reason"))
            why = _why(reason if isinstance(reason, str) else None, None)
            detail = f" — {escape(why)}" if event in {"warning", "suspended"} else ""
            if event == "warning":
                errors = _optional_positive_int(
                    product.get("error_count", payload.get("error_count"))
                )
                maximum = _optional_positive_int(
                    product.get("max_errors", payload.get("max_errors"))
                )
                if errors is not None and maximum is not None:
                    detail += f" ({errors}/{maximum})"
            details.append(f"#{pid}: {name}{detail}")
        if len(product_rows) > MAX_LISTED_PRODUCTS:
            details.append(_("… and {k} more").format(k=len(product_rows) - MAX_LISTED_PRODUCTS))
        if details:
            row += "\n" + "\n".join(details)
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
        if payload.get("kind") in {"operational", "availability"}:
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
        self._flush_locks: dict[int, asyncio.Lock] = {}

    async def enqueue(
        self, *, user_id: int, product_id: int | None, payload: dict[str, Any]
    ) -> int:
        """Enqueue an alert payload for digest delivery.

        The ``digest_pending`` skip metric is emitted by the caller
        (TelegramNotifier) — not here — so one routed alert counts once.
        """
        async with self._flush_locks.setdefault(user_id, asyncio.Lock()):
            # Keep only live status rows even when mute/quiet hours postpone all delivery.
            await self._current_entries(user_id=user_id, apply_preferences=False)
            raw_id = payload.get("event_id")
            return await self._repo.enqueue_digest(
                user_id=user_id,
                product_id=product_id,
                payload=json.dumps(payload),
                event_id=raw_id if isinstance(raw_id, str) else None,
            )

    async def _current_status_payload(
        self, payload: dict[str, Any], *, user_id: int
    ) -> dict[str, Any] | None:
        """Remove obsolete members using both the stored version and current state."""
        event = payload.get("event")
        if event == "quarantine":
            # Domain breaker state belongs to the scheduler. A deferred notice
            # cannot promise that the quarantine is still current at delivery.
            return {**payload, "historical": True}
        if event not in {"suspended", "unavailable", "restocked", "warning"}:
            return payload
        versioned = payload.get("status_versioned") is True
        products = payload.get("products")
        if not isinstance(products, list) or not products:
            return None if versioned else {**payload, "historical": True}
        current = []
        for item in products:
            if not isinstance(item, dict):
                continue
            pid = _optional_positive_int(item.get("id"))
            version = _optional_positive_int(item.get("notice_version"))
            if pid is None or (versioned and version is None):
                continue
            record = await self._repo.get_product_for_user(pid, user_id)
            if record is None or (versioned and record.notice_version != version):
                continue
            matches = (
                (
                    event == "suspended"
                    and not record.is_active
                    and record.suspension_kind == "automatic"
                )
                or (event == "unavailable" and record.is_active and not record.is_available)
                or (event == "restocked" and record.is_active and record.is_available)
                or (
                    event == "warning"
                    and record.is_active
                    and record.consecutive_errors > 0
                    and record.last_check_outcome == item.get("reason", payload.get("reason"))
                )
            )
            if matches:
                current.append(
                    {
                        **item,
                        "name": record.name or record.url,
                        "reason": record.suspension_reason
                        if event == "suspended"
                        else (record.last_check_outcome if event == "warning" else event),
                        "error_count": record.consecutive_errors,
                        "last_price": str(record.current_price)
                        if record.current_price is not None
                        else None,
                        "currency": record.currency,
                    }
                )
        if not current:
            return None
        reasons = {item["reason"] for item in current}
        return {
            **payload,
            "products": current,
            "product_ids": [p["id"] for p in current],
            "count": len(current),
            "reason": next(iter(reasons)) if len(reasons) == 1 else None,
            "product_name": current[0]["name"]
            if len(current) == 1
            else payload.get("product_name"),
            "last_price": current[0]["last_price"] if len(current) == 1 else None,
            "currency": current[0]["currency"] if len(current) == 1 else None,
        }

    async def _current_entries(
        self, *, user_id: int, apply_preferences: bool = True
    ) -> list[DigestEntry]:
        """Retire stale status rows; defer live entries muted or quiet at actual delivery."""
        entries = await self._repo.list_pending_digest(user_id=user_id)
        manager = PreferencesManager(self._repo)
        now = datetime.now(UTC)
        kept = []
        obsolete = []
        seen_events: set[str] = set()
        seen_statuses: set[tuple[int, int]] = set()
        for entry in entries:
            try:
                payload = json.loads(entry.alert_payload_json)
            except (TypeError, json.JSONDecodeError):
                kept.append(entry)  # existing unreadable-row quarantine owns these
                continue
            if not isinstance(payload, dict):
                kept.append(entry)
                continue
            current = await self._current_status_payload(payload, user_id=user_id)
            event_id = payload.get("event_id")
            if isinstance(event_id, str) and event_id in seen_events:
                current = None
            if current is not None and current.get("status_versioned") is True:
                members = []
                for item in current["products"]:
                    identity = (item["id"], item["notice_version"])
                    if identity not in seen_statuses:
                        members.append(item)
                        seen_statuses.add(identity)
                current = (
                    {**current, "products": members, "count": len(members)} if members else None
                )
            if current is None:
                if entry.id is not None:
                    obsolete.append(entry.id)
                continue
            if isinstance(event_id, str):
                seen_events.add(event_id)
            if apply_preferences:
                global_prefs = await manager.resolve_global(user_id=user_id)
                operational = current.get("kind") == "operational"
                effective = (
                    await manager.resolve(user_id=user_id, product_id=entry.product_id)
                    if not operational and entry.product_id is not None
                    else global_prefs
                )
                if is_quiet_now(global_prefs, now_utc=now) or is_quiet_now(effective, now_utc=now):
                    continue
                if not operational and is_muted_now(effective, now_utc=now):
                    continue
            kept.append(replace(entry, alert_payload_json=json.dumps(current)))
        if obsolete:
            await self._repo.mark_digest_flushed(obsolete)
        return kept

    async def flush_user(self, *, user_id: int) -> int:
        """Flush all pending digest entries for a single user. Returns count flushed."""
        async with self._flush_locks.setdefault(user_id, asyncio.Lock()):
            return await self._flush_user_unlocked(user_id=user_id)

    async def pending_count(self, *, user_id: int) -> int:
        """Entries retained after delivery, including those deferred by preferences."""
        return len(await self._repo.list_pending_digest(user_id=user_id))

    async def _flush_user_unlocked(self, *, user_id: int) -> int:
        entries = await self._current_entries(user_id=user_id)
        if not entries:
            return 0
        token = set_locale(await user_locale(self._repo, user_id, self._lang))
        try:
            header, blocks, footer, unrenderable_ids = _digest_blocks(entries)
        finally:
            reset_locale(token)
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
                try:
                    flushed_total += await self.flush_user(user_id=user_id)
                except Exception:  # noqa: BLE001 - one unreachable recipient cannot stop others
                    log.exception("Digest delivery failed for user %d", user_id)
        return flushed_total
