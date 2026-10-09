"""List only features: what users can now do and what works better for them.
Exclude translations, branding/docs/repo, refactors, CI/tests, hardening, compatibility promises.
Keep each phrase and its translations within 30 display cells, leaving two for the bullet.
"""

from __future__ import annotations

from html import escape

from price_tracker.bot.messages import N_, _
from price_tracker.bot.ui.width import truncate_to_width

# Oldest first.
RELEASE_NOTES: dict[str, tuple[str, ...]] = {
    "1.0.0": (),
    "1.1.0": (
        N_("90 days of price charts"),
        N_("Grouped site warnings"),
        N_("Digests after quiet hours"),
    ),
    "1.2.0": (
        N_("Cancel prompts with /cancel"),
        N_("Guided prices and checks"),
    ),
    "1.3.0": (
        N_("Check each product your way"),
        N_("Product actions on each card"),
        N_("Clearer list import feedback"),
    ),
    "1.4.0": (
        N_("Product pages and filters"),
        N_("No price alerts for sold-outs"),
    ),
    "1.4.1": (N_("Main menu grouped by category"),),
    "1.4.2": (
        N_("Digests count failed checks"),
        N_("Clearer unsupported-link help"),
    ),
    "1.5.0": (
        N_("Filter sold-out products"),
        N_("Product cards stay in place"),
    ),
    "1.6.0": (
        N_("Simpler command menu"),
        N_("Main menu via /start"),
        N_("No repeated suspension notices"),
    ),
    "1.7.0": (
        N_("Every command has a button"),
        N_("Guided notification settings"),
        N_("Alerts for each product"),
    ),
    "1.7.1": (N_("One alert when back in stock"),),
    "1.8.0": (
        N_("Lists fit on one line"),
        N_("What’s new after each update"),
    ),
    "1.8.1": (),
}

MESSAGE_LIMIT = 1200


def version_key(version: str) -> tuple[int, ...]:
    """Compare numeric release components, including minor versions above nine."""
    return tuple(int(part) for part in version.split("."))


def _escaped_short(text: str, limit: int, width: int = 32) -> str:
    """Bound visible width before escaping, then cap encoded length safely."""
    text = truncate_to_width(text, width)
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
    newest = [f"• {_escaped_short(_(bullet), 230, 30)}" for bullet in RELEASE_NOTES[current]]
    message = "\n".join([title, *newest])
    older = sorted(
        (
            version
            for version in RELEASE_NOTES
            if RELEASE_NOTES[version]
            and version_key(previous) < version_key(version) < version_key(current)
        ),
        key=version_key,
        reverse=True,
    )
    if not newest and not older:
        return ""
    if not older:
        return message
    heading = _escaped_short(_("Earlier:"), 120)
    suffix = _escaped_short(_("…and earlier improvements."), 100)
    lines = [f"• {_escaped_short(_(RELEASE_NOTES[version][0]), 230, 30)}" for version in older]
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
