"""Ordered, translatable release highlights shown once after an update."""

from __future__ import annotations

from html import escape

from price_tracker.bot.messages import N_, _

# Oldest first. Every release must add two to four short benefits for users.
RELEASE_NOTES: dict[str, tuple[str, ...]] = {
    "1.0.0": (
        N_("Keep your tracked products and familiar commands through 1.x updates."),
        N_("Blocked sites pause automatically, avoiding repeated failed checks."),
    ),
    "1.1.0": (
        N_("See 90 days of price history in your charts."),
        N_("Get grouped warnings when a site has trouble, with quick recovery buttons."),
        N_("Digests wait until your quiet hours end."),
    ),
    "1.2.0": (
        N_("Cancel a pending question with /cancel; unanswered questions expire."),
        N_("Set price rules and check intervals with clearer prompts in your language."),
    ),
    "1.3.0": (
        N_("Choose how often each product is checked."),
        N_("Manage products from cards with check, history and pause buttons."),
        N_("Import product lists with clearer feedback on invalid rows."),
    ),
    "1.4.0": (
        N_("Choose your language in Settings, including for price alerts."),
        N_("Browse products by page and filter active, paused or failing items."),
        N_("Sold-out listings no longer trigger misleading price alerts."),
    ),
    "1.4.1": (
        N_("Find Prices and Notifications again in the grouped main menu."),
        N_("Menu icons now match their actions."),
    ),
    "1.4.2": (
        N_("Digests show the actual number of failed checks."),
        N_("Unsupported product links get a clearer explanation."),
    ),
    "1.5.0": (
        N_("Filter sold-out products and see availability on each card."),
        N_("Product cards stay in place after actions, with a short confirmation."),
    ),
    "1.6.0": (
        N_("Reach everyday actions from a simpler Telegram command menu."),
        N_("Open the main menu with /start and find Help under Status & info."),
        N_("Automatic suspension notices no longer repeat during overlapping checks."),
    ),
    "1.7.0": (
        N_("Reach every command through buttons in the main menu."),
        N_("Set quiet hours, time zone and notification limits with guided prompts."),
        N_("Manage one product's notifications without changing your other settings."),
    ),
    "1.7.1": (
        N_("Get just one back-in-stock alert when a sold-out product returns."),
        N_("English replies now stay in English throughout the bot."),
        N_("Buttons in older messages keep working after updates."),
    ),
}

MESSAGE_LIMIT = 1200


def version_key(version: str) -> tuple[int, ...]:
    """Compare numeric release components, including minor versions above nine."""
    return tuple(int(part) for part in version.split("."))


def _escaped_short(text: str, limit: int) -> str:
    """Bound escaped text without cutting an HTML entity or a Unicode character."""
    if len(escape(text)) <= limit:
        return escape(text)
    parts: list[str] = []
    remaining = limit - 1
    for character in text:
        encoded = escape(character)
        if len(encoded) > remaining:
            break
        parts.append(encoded)
        remaining -= len(encoded)
    return "".join(parts) + "…"


def render_release_notes(previous: str, current: str) -> str:
    """Render the current release and one line per skipped release, newest first."""
    title = _escaped_short(_("Updated to version {version}").format(version=current), 160)
    newest = [f"• {_escaped_short(_(bullet), 230)}" for bullet in RELEASE_NOTES[current]]
    message = "\n".join([title, *newest])
    older = sorted(
        (
            version
            for version in RELEASE_NOTES
            if version_key(previous) < version_key(version) < version_key(current)
        ),
        key=version_key,
        reverse=True,
    )
    if not older:
        return message
    heading = _escaped_short(_("Also since your last update:"), 120)
    suffix = _escaped_short(_("…and earlier improvements."), 100)
    lines = [f"• {escape(version)}: {escape(_(RELEASE_NOTES[version][0]))}" for version in older]
    prefix = f"{message}\n\n{heading}\n"
    omitted = False
    while (
        lines
        and len(prefix + "\n".join(lines) + ("\n" + suffix if omitted else "")) > MESSAGE_LIMIT
    ):
        lines.pop()
        omitted = True
    if not lines:
        return message + "\n" + suffix
    return prefix + "\n".join(lines) + ("\n" + suffix if omitted else "")
