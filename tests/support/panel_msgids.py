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
    "⏱ Throttle: {throttle} · change with /throttle &lt;N&gt;|off",
    "🌍 Timezone: {timezone} · change with /timezone &lt;zone&gt;",
    "Other values: /digest_mode on|off &lt;minutes&gt;, /quiet_hours HH:MM-HH:MM",
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
    "◀️ Settings",
    "📦 <b>Your products</b> · {filter} ({total}) · page {page}/{pages}",
    "errors",
    "{mark}<b>#{id}</b> {name} · {price}",
    "Nothing here.",
    "✅ Active",
    "⏸ Paused",
    "⚠️ Errors",
    "🗑 Delete all",
    "📦 {active} · ⏸ {paused}",
    "{n} paused",
    "🏠 <b>Price Tracker</b>\n\n{counts}\n\nPaste a product link to start tracking.",
    "📦 Products",
    "🔍 Check all",
    "📊 Statistics",
    "💾 Data",
    "⚙️ Settings",
    "👑 Admin",
    "Product not found.",
)

PANEL_PLURAL: tuple[str, str] = ("{n} active", "{n} active")

PANEL_IDENTICAL: frozenset[str] = frozenset(
    {
        "📬 Digest: {digest}",
        "off",
        "📬 Digest",
        "🏠 Home",
        "📬 <b>Digest</b>",
        "{mark}<b>#{id}</b> {name} · {price}",
        "📦 {active} · ⏸ {paused}",
        "👑 Admin",
    }
)
