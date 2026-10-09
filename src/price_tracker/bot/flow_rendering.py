"""Prompts, translated rendering, and input parsing for guided flows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from price_tracker.app.inputs import (
    InputError,
    InputErrorCode,
    parse_digest_interval,
    parse_mute_hours,
    parse_product_interval,
    parse_quiet_hours,
    parse_target,
    parse_threshold,
    parse_throttle,
    parse_timezone,
)
from price_tracker.bot.flow_state import (
    URL_PATTERN,
    ActiveFlow,
    ApplyStatus,
    FlowKind,
    FlowState,
    PrepareStatus,
)
from price_tracker.bot.messages import N_, _

if TYPE_CHECKING:
    from collections.abc import Callable

# --- texts ------------------------------------------------------------------
#
# Every text is an English msgid marked with ``N_`` and translated with ``_`` at
# the moment it is sent, in the language chosen for the update or the timeout.

TEXT_EXPIRED: Final = N_("This button has expired.")
TEXT_NOT_AUTHORISED: Final = N_("Not authorised.")
TEXT_NOT_FOUND: Final = N_("Product not found.")
TEXT_SUPERSEDED: Final = N_("Replaced by a newer prompt.")
TEXT_NO_OPEN_PROMPT: Final = N_("No open prompt - use the buttons or /menu.")
TEXT_NOTHING_TO_CANCEL: Final = N_("Nothing to cancel.")
TEXT_TOO_MANY: Final = N_("Too many invalid answers - cancelled, nothing changed.")
TEXT_SAVED: Final = N_("Saved.")
TEXT_ADDED: Final = N_("Added.")
TEXT_ADDED_OTHER_STORES: Final = N_("Added - other stores will be followed too.")
TEXT_SCRAPE_FAILED: Final = N_("Could not read this page. Nothing was added.")
TEXT_ALREADY_TRACKED: Final = N_("Already tracked.")
TEXT_RESUMED: Final = N_("Tracking resumed.")
TEXT_TYPE_CODE: Final = N_("Type a three-letter currency code, for example USD.")
TEXT_CANCELLED: Final = N_("Cancelled - nothing changed.")
TEXT_NO_PRODUCT: Final = N_("Cancelled - no product was added.")
TEXT_EXPIRED_VALUE: Final = N_("Expired - nothing changed.")
TEXT_EXPIRED_ADD: Final = N_("Expired - no product was added.")
TEXT_SCOPE_NOTICE: Final = N_("Other stores will be followed at this level from a later version.")
TEXT_WHICH_CURRENCY: Final = N_("Which currency is this price in?")
TEXT_WHERE_FOLLOWED: Final = N_("Where should the price be followed?")
TEXT_CHOOSE_LEVEL: Final = N_("Choose a level:")
TEXT_CURRENCY_CHOSEN: Final = N_("Currency: {code}")
LABEL_CANCEL: Final = N_("Cancel")
LABEL_BACK_SETTINGS: Final = N_("⬅️ Settings")
LABEL_BACK_NOTIFICATIONS: Final = N_("⬅️ Notifications")
LABEL_HOME: Final = N_("🏠 Home")
LABEL_TYPE_CODE: Final = N_("Type a code")
LABEL_OTHER_STORES: Final = N_("Other stores too")
LABEL_ONLY_ON: Final = N_("Only on {store}")
_SCOPE_LEVEL_LABELS: Final = (
    ("own_country", N_("My country")),
    ("customs_area", N_("My customs area")),
    ("world", N_("Worldwide")),
)

_PROMPTS: Final = {
    FlowKind.THRESHOLD: N_("Send the drop threshold: 20% or 5.50 (one dot or comma)."),
    FlowKind.TARGET: N_("Send the target price, e.g. 49.90 (0 clears it)."),
    FlowKind.INTERVAL: N_("Send the check interval in minutes (5-10080, 0 resets)."),
    FlowKind.MUTE: N_("Send the mute duration in hours (1-8760), or forever."),
    FlowKind.DIGEST: N_("Send the digest interval in minutes (5-1440)."),
    FlowKind.QUIET: N_("Send the quiet hours as HH:MM-HH:MM, or off."),
    FlowKind.TIMEZONE: N_("Send your time zone, for example Europe/Rome."),
    FlowKind.THROTTLE: N_("Send the most notifications per hour, or off."),
}
PROMPT_DEBUG: Final = N_("Send the link of the product page to analyse.")

_HINTS: Final = {
    InputErrorCode.EMPTY: N_("Please send a value."),
    InputErrorCode.TOO_LONG: N_("That is too long."),
    InputErrorCode.CONTROL_CHARACTER: N_("That contains invisible characters."),
    InputErrorCode.NOT_A_NUMBER: N_("Use digits with one dot or comma, e.g. 1299.99."),
    InputErrorCode.OUT_OF_RANGE: N_("That value is out of range."),
    InputErrorCode.NOT_A_CURRENCY: N_("Unknown currency code."),
    InputErrorCode.NOT_A_TIMEZONE: N_("Unknown time zone."),
    InputErrorCode.NOT_A_TIME_RANGE: N_("Use HH:MM-HH:MM."),
    InputErrorCode.NOT_A_URL: N_("That is not a link."),
    InputErrorCode.UNSAFE_URL: N_("That link is not allowed."),
}
# The interval prompt takes whole minutes only: the generic number hint (one dot
# or comma, e.g. 1299.99) would steer the user to another rejected answer.
TEXT_WHOLE_MINUTES: Final = N_("Use whole minutes, e.g. 30.")
# The same for the settings that take a whole number of hours, minutes or messages.
TEXT_WHOLE_NUMBER: Final = N_("Use a whole number, e.g. 12.")
_WHOLE_NUMBER_KINDS: Final = frozenset({FlowKind.MUTE, FlowKind.DIGEST, FlowKind.THROTTLE})


def _kept_text(store: str) -> str:
    return _("Kept: only on {store}.").format(store=store)


def closing_text(flow: ActiveFlow, *, expired: bool = False) -> str:
    """The text a prompt is edited to when its flow ends without an answer, translated."""
    if flow.state is FlowState.AWAIT_SCOPE:
        return _kept_text(flow.store)
    if flow.state is FlowState.AWAIT_CURRENCY:
        return _(TEXT_EXPIRED_ADD if expired else TEXT_NO_PRODUCT)
    return _(TEXT_EXPIRED_VALUE if expired else TEXT_CANCELLED)


def _parse_link(text: str) -> str | InputError:
    """The first link in ``text``, without trailing punctuation."""
    match = URL_PATTERN.search(text)
    if match is not None:
        url = match.group(0).rstrip(".,;:!?)")
        if URL_PATTERN.fullmatch(url):
            return url
    return InputError(InputErrorCode.NOT_A_URL)


_PARSERS: Final[dict[FlowKind, Callable[[str], object]]] = {
    FlowKind.DEBUG: _parse_link,
    FlowKind.THRESHOLD: parse_threshold,
    FlowKind.TARGET: parse_target,
    FlowKind.INTERVAL: parse_product_interval,
    FlowKind.MUTE: parse_mute_hours,
    FlowKind.DIGEST: parse_digest_interval,
    FlowKind.QUIET: parse_quiet_hours,
    FlowKind.TIMEZONE: parse_timezone,
    FlowKind.THROTTLE: parse_throttle,
}


def _parse_for(kind: FlowKind, text: str) -> object:
    return _PARSERS[kind](text)


_APPLY_TEXTS: Final = {
    ApplyStatus.OK: TEXT_SAVED,
    ApplyStatus.NOT_AUTHORISED: TEXT_NOT_AUTHORISED,
    ApplyStatus.NOT_FOUND: TEXT_NOT_FOUND,
}

_PREPARE_TEXTS: Final = {
    PrepareStatus.FAILED: TEXT_SCRAPE_FAILED,
    PrepareStatus.DUPLICATE_ACTIVE: TEXT_ALREADY_TRACKED,
    PrepareStatus.DUPLICATE_REACTIVATED: TEXT_RESUMED,
    PrepareStatus.NOT_AUTHORISED: TEXT_NOT_AUTHORISED,
}
