"""The closed list of msgids the settings panels, the list and the Home screen added.

Shared between ``tests/i18n/test_nav_catalog.py`` (verifies the source and
compiled catalogs) and ``tests/ui/test_snapshots.py`` (verifies the pseudo
catalog used for the locales without a real translation).
"""

from __future__ import annotations

PANEL_SINGULAR: tuple[str, ...] = (
    "⚙️ <b>Settings</b>",
    "🔕 Mute: {mute}",
    "📬 Digest: {digest}",
    "🌙 Quiet hours: {quiet}",
    "🚦 Throttle: {throttle}",
    "🌍 Timezone: {timezone}",
    "off",
    "forever",
    "until {when}",
    "on, every {interval}",
    "{n} per hour",
    "unlimited",
    "🔕 Mute",
    "📬 Digest",
    "🌙 Quiet hours",
    "🏠 Home",
    "🔕 <b>Mute</b>",
    "📬 <b>Digest</b>",
    "🌙 <b>Quiet hours</b>",
    "Current: {value}",
    "Forever",
    "🔔 Unmute",
    "On",
    "Off",
    "⬅️ Settings",
    "📦 <b>Your products</b> · {filter} ({total}) · page {page}/{pages}",
    "errors",
    "N/A",
    "Unknown",
    "Nothing here.",
    "✅ Active",
    "⏸ Paused",
    "⚠️ Errors",
    "🗑 Delete all",
    "📦 {active} · ⏸ {paused}",
    "{n} paused",
    "🏠 <b>Price Tracker</b>\n\n{counts}\n\nPaste a product link to start tracking.",
    "📦 Products",
    "💶 Prices",
    "🔔 Notifications",
    "ℹ️ Status & info",
    "💾 Data",
    "⚙️ Settings",
    "👑 Admin",
    "Product not found.",
    "🗣 Language: {language}",
    "🗣 Language",
    "🗣 <b>Language</b>",
    "Automatic",
    "Automatic ({language})",
    "sold out",
    "🚫 Sold out",
    "🚦 Throttle",
    "🌍 Timezone",
    "🚦 <b>Throttle</b>",
    "🌍 <b>Timezone</b>",
    "📨 Send now",
    "🔔 <b>Notifications</b>",
    "⬅️ Product",
    "Pending alerts sent: {n}",
    "⬅️ Admin",
    "⬅️ Status & info",
    "➕ Add",
    "✏️ Other duration",
    "✏️ Interval",
    "✏️ Other hours",
    "✏️ Change",
    "➕ <b>Add a product</b>\n\nPaste the link of a product page here to start tracking it.",
)

PANEL_PLURAL: tuple[str, str] = ("{n} active", "{n} active")

PANEL_IDENTICAL: frozenset[str] = frozenset(
    {
        "📬 Digest: {digest}",
        "off",
        "📬 Digest",
        "🏠 Home",
        "📬 <b>Digest</b>",
        "📦 {active} · ⏸ {paused}",
        "👑 Admin",
        "⬅️ Admin",
    }
)
