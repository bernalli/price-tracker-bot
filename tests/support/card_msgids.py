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
    "💰 Last recorded price: {price}",
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
    "⚠️ {reason}",
    "🚫 Last confirmed: sold out",
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

# Outcome explanations now live in bot/ui/check_status.py and are extracted
# into the same catalogs as the card and navigation screens.
STATUS_SINGULAR: tuple[str, ...] = (
    "A price was found and needs another check before it is saved.",
    "Check postponed while the site is being tested.",
    "Check postponed while the site is temporarily restricted.",
    "No offer matched the selected condition.",
    "Price updated; availability could not be verified.",
    "Not checked: tracking is paused.",
    "The check could not be completed because of a communication or processing error.",
    "The check could not be completed.",
    "The price could not be read.",
    "The price currency could not be determined.",
    "The price reading could not be accepted.",
    "The price uses a different currency and was not saved.",
    "The product is currently unavailable.",
    "The product page could not be found. Check the link.",
    "The product was not found.",
    "The site asked to wait before checking again.",
    "The site is blocking automated checks.",
    "This site cannot be checked right now.",
)

CARD_PLURAL: tuple[str, str] = (
    "⚠️ {n} failed read · /errors",
    "⚠️ {n} failed reads · /errors",
)

CARD_IDENTICAL: frozenset[str] = frozenset(
    {"📦 <b>{name}</b>", "{domain} · #{id} · {status}", "⚠️ {reason}"}
)
