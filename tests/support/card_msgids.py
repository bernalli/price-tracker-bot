"""The closed list of msgids the product card catalog entries added.

Shared between ``tests/i18n/test_card_catalog.py`` (verifies the compiled
and source catalogs) and ``tests/ui/test_snapshots.py`` (verifies the
pseudo-catalog used for the seven locales without a real translation).
"""

from __future__ import annotations

CARD_SINGULAR: tuple[str, ...] = (
    "📦 <b>{name}</b>",
    "{domain} · #{id} · {status}",
    "active",
    "active ones",
    "paused",
    "suspended",
    "💰 Now: —",
    "💰 Now: {price}",
    "💰 Now: {price} ≈ {estimate}",
    "📌 Start: {price}",
    "📌 Start: {price} · {change}",
    "📉 Lowest: {price}",
    "🎯 Target: {price}",
    "🔔 Alert: any drop",
    "🔔 Alert: drop of {percent} from start",
    "🔔 Alert: drop of {amount} from start",
    "🔔 Alert: at or below {price}",
    "⏱ Checks: every {interval} · never",
    "⏱ Checks: every {interval} · just now",
    "⏱ Checks: every {interval} · last {ago}",
    "🔄 Check now",
    "📈 History",
    "⏸ Pause",
    "▶️ Reactivate",
    "🗑 Delete",
    "🔔 Alert rule",
    "⏱ Interval",
    "🔗 Open",
    "⬅️ List",
)

CARD_PLURAL: tuple[str, str] = (
    "⚠️ {n} failed read · /errors",
    "⚠️ {n} failed reads · /errors",
)

CARD_IDENTICAL: frozenset[str] = frozenset({"📦 <b>{name}</b>", "{domain} · #{id} · {status}"})
