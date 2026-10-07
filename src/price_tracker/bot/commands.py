"""The closed registry of bot commands.

One list feeds the Telegram command menus, the ``/help`` screen and the test that
ties it to the registered handlers. Descriptions are English msgids marked with
``N_`` and translated with ``_`` (or a catalogue's ``gettext``) when shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from price_tracker.bot.messages import N_

GROUPS: Final = ("track", "alerts", "data", "admin")

GROUP_TITLES: Final = {
    "track": N_("📦 Products"),
    "alerts": N_("🔔 Alerts"),
    "data": N_("💾 Data"),
    "admin": N_("👑 Admin"),
}

HELP_HEADER: Final = N_("❓ <b>Commands</b>")


@dataclass(frozen=True)
class CommandSpec:
    """A canonical command: its name, English description and help group."""

    name: str
    description: str
    group: str

    @property
    def admin(self) -> bool:
        """Whether only administrators may run it (the ``admin`` group)."""
        return self.group == "admin"


COMMANDS: Final = (
    CommandSpec("start", N_("Start the bot"), "track"),
    CommandSpec("menu", N_("Open the main menu"), "track"),
    CommandSpec("help", N_("Show every command"), "track"),
    CommandSpec("cancel", N_("Cancel the current action"), "track"),
    CommandSpec("add", N_("Track a product from a link"), "track"),
    CommandSpec("list", N_("List your products"), "track"),
    CommandSpec("check", N_("Check a product now"), "track"),
    CommandSpec("checkall", N_("Check all your products now"), "track"),
    CommandSpec("refresh", N_("Set how often a product is checked"), "track"),
    CommandSpec("pause", N_("Pause tracking a product"), "track"),
    CommandSpec("reactivate", N_("Resume tracking a product"), "track"),
    CommandSpec("delete", N_("Delete a product"), "track"),
    CommandSpec("history", N_("Show a product's price chart"), "track"),
    CommandSpec("reset", N_("Reset a product's starting price"), "track"),
    CommandSpec("target", N_("Set or clear a target price"), "alerts"),
    CommandSpec("threshold", N_("Set when to alert about a drop"), "alerts"),
    CommandSpec("mute", N_("Mute notifications"), "alerts"),
    CommandSpec("unmute", N_("Unmute notifications"), "alerts"),
    CommandSpec("digest_mode", N_("Turn the notification digest on or off"), "alerts"),
    CommandSpec("quiet_hours", N_("Set quiet hours"), "alerts"),
    CommandSpec("timezone", N_("Set your timezone"), "alerts"),
    CommandSpec("throttle", N_("Limit notifications per hour"), "alerts"),
    CommandSpec("prefs", N_("Show your notification settings"), "alerts"),
    CommandSpec("digest_now", N_("Send the pending digest now"), "alerts"),
    CommandSpec("export", N_("Export your products as CSV"), "data"),
    CommandSpec("import", N_("Import products from a CSV file"), "data"),
    CommandSpec("status", N_("Show your statistics"), "data"),
    CommandSpec("errors", N_("Show products with errors"), "data"),
    CommandSpec("adduser", N_("Authorize a user"), "admin"),
    CommandSpec("removeuser", N_("Remove a user"), "admin"),
    CommandSpec("users", N_("List authorized users"), "admin"),
    CommandSpec("nick", N_("Set a user's nickname"), "admin"),
    CommandSpec("setinterval", N_("Set the global check interval"), "admin"),
    CommandSpec("debug", N_("Debug the scraping of a link"), "admin"),
    CommandSpec("health", N_("Show scraper health"), "admin"),
)

# Registered handlers that share a canonical command's behaviour; never listed in a menu.
ALIAS_COMMANDS: Final = (
    "aggiungi",
    "lista",
    "controlla",
    "pausa",
    "riattiva",
    "elimina",
    "storia",
    "azzera",
    "soglia",
    "esporta",
    "importa",
    "stato",
    "errori",
    "utenti",
    "intervallo",
)
