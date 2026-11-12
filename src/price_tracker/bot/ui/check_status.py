"""Safe, localized explanations for interactive check outcomes."""

from __future__ import annotations

from price_tracker.bot.messages import N_, _

# Outcomes of a check the store refused or the bot skipped because the store is
# paused. They are not failures of the product and carry no error counter, but
# the product is not being read: lists and reports show them with the errors.
STORE_REFUSAL_OUTCOMES = frozenset({"block", "quarantined", "probe_skipped"})

_REASONS = {
    "block": N_("The site is blocking automated checks."),
    "quarantined": N_("Check postponed while the site is temporarily restricted."),
    "not_found": N_("The product was not found."),
    "probe_skipped": N_("Check postponed while the site is being tested."),
    "deferred": N_("The site asked to wait before checking again."),
    "inactive": N_("Not checked: tracking is paused."),
    "no_scraper": N_("This site cannot be checked right now."),
    "price_none": N_("The price could not be read."),
    "parse_error": N_("The price could not be read."),
    "condition_mismatch": N_("No offer matched the selected condition."),
    "availability_unknown": N_("Price updated; availability could not be verified."),
    "currency_unknown": N_("The price currency could not be determined."),
    "currency_mismatch": N_("The price uses a different currency and was not saved."),
    "implausible_read": N_("The price reading could not be accepted."),
    "awaiting_confirmation": N_("A price was found and needs another check before it is saved."),
    "listing_unconfirmed": N_("The product page could not be found. Check the link."),
    "listing_gone": N_("The product page could not be found. Check the link."),
    "http_error": N_(
        "The check could not be completed because of a communication or processing error."
    ),
    "unexpected": N_("The check could not be completed."),
    "out_of_stock": N_("The product is currently unavailable."),
    "unavailable": N_("The product is currently unavailable."),
}


def check_reason(reason: str | None) -> str:
    """Return a closed-set explanation; never render scraper or exception text."""
    return _(_REASONS.get(reason or "", N_("The check could not be completed.")))
