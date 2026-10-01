"""The currency engine: ISO codes, symbol-based detection, regional parsing.

Not wired into anything yet: no caller changes, no delegation from
``scraper_base``. ``detect_currency`` reuses the grammar's own tokenizer
(``pricegrammar._strip_currency_token``) instead of a second one, because two
tokenizers would disagree; it strips at most two tokens (one at each end),
combines what they say, and returns a single code only when the signals agree
or the caller's expectation breaks a tie between exactly two remaining
candidates. It never returns a default: an unresolved case is ``None``, not a
guess. ``expected_currencies`` derives an expectation from a domain, giving no
expectation at all for a generic ccTLD or an IDN suffix rather than
a wrong one: a wrong expectation would make a shared symbol resolve to the
wrong currency and reject a correct price at the cross-check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from babel.numbers import get_territory_currencies

from price_tracker.core import identity, money, pricegrammar
from price_tracker.core.money import Money
from price_tracker.core.pricegrammar import PriceContext

ISO_CURRENCIES: Final = money.ACCEPTED_CURRENCIES

# ccTLDs sold and used as generic brand domains, not as a signal of a region's currency
# These suffixes deliberately provide no regional currency expectation.
GENERIC_CCTLDS: Final[frozenset[str]] = frozenset(
    {
        "ad",
        "ai",
        "as",
        "bz",
        "cc",
        "cd",
        "co",
        "dj",
        "fm",
        "io",
        "la",
        "me",
        "ms",
        "nu",
        "sc",
        "sr",
        "su",
        "tv",
        "tk",
        "ws",
    }
)
NO_EXPECTATION_TLDS: Final[frozenset[str]] = GENERIC_CCTLDS | frozenset({"eu"})
_CCTLD_TERRITORY_EXCEPTIONS: Final[dict[str, str]] = {"uk": "GB"}

REGIONAL_MISS_REASONS: Final = frozenset({"unreadable", "currency_unexpected"})


@dataclass(frozen=True, slots=True)
class RegionalMiss:
    """A regional price read that failed, and why."""

    reason: str

    def __post_init__(self) -> None:
        if self.reason not in REGIONAL_MISS_REASONS:
            raise ValueError(f"unknown regional-miss reason {self.reason!r}")


def _validate_currency_set(value: object, *, name: str, allow_empty: bool) -> frozenset[str]:
    if type(value) is not frozenset:
        raise ValueError(f"bad_{name}")
    if not allow_empty and not value:
        raise ValueError(f"bad_{name}")
    for code in value:
        if type(code) is not str or code not in ISO_CURRENCIES:
            raise ValueError(f"bad_{name}")
    return value


def detect_currency(text: object, *, expected: frozenset[str] = frozenset()) -> str | None:
    """Detect a single currency from at most two tokens of ``text``.

    Total on ``text``: never raises, whatever its type or content. ``expected``
    is a trusted argument: a non-``frozenset`` or a member outside
    :data:`ISO_CURRENCIES` raises ``ValueError('bad_expected')``.
    """
    _validate_currency_set(expected, name="expected", allow_empty=True)
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped or len(stripped) > pricegrammar.MAX_TEXT_LENGTH:
        return None

    signals: list[frozenset[str]] = []
    residue, iso, candidates = pricegrammar._strip_currency_token(stripped)
    if iso is None and candidates is None:
        return None
    if iso is not None:
        signals.append(frozenset({iso}))
    else:
        assert candidates is not None
        signals.append(candidates & ISO_CURRENCIES)

    _residue2, iso2, candidates2 = pricegrammar._strip_currency_token(residue)
    if iso2 is not None:
        signals.append(frozenset({iso2}))
    elif candidates2 is not None:
        signals.append(candidates2 & ISO_CURRENCIES)

    combined = signals[0]
    for signal in signals[1:]:
        combined = combined & signal
    if len(combined) == 1:
        return next(iter(combined))
    if len(combined) > 1:
        intersection = combined & expected
        if len(intersection) == 1:
            return next(iter(intersection))
    return None


def expected_currencies(url: object, *, declared: frozenset[str] | None = None) -> frozenset[str]:
    """Currencies a page at ``url`` is expected to price in.

    ``declared`` is a trusted argument: when given it must be a non-empty
    ``frozenset`` of codes in :data:`ISO_CURRENCIES`, and it wins over the
    domain. Total on ``url``: never raises, whatever its type or content.
    """
    if declared is not None:
        return _validate_currency_set(declared, name="declared", allow_empty=False)
    normalized = identity.normalize_url(url)
    if normalized is None:
        return frozenset()
    host = urlsplit(normalized).hostname or ""
    suffix = identity._extractor(host).suffix.lower()
    if not suffix:
        return frozenset()
    label = suffix.rsplit(".", 1)[-1]
    if label in NO_EXPECTATION_TLDS:
        return frozenset()
    if not (len(label) == 2 and label.isascii() and label.isalpha()):
        return frozenset()
    territory = _CCTLD_TERRITORY_EXCEPTIONS.get(label, label.upper())
    return frozenset(get_territory_currencies(territory)) & ISO_CURRENCIES


def parse_regional_price(
    text: object,
    *,
    url: object,
    declared: frozenset[str] | None = None,
    digits: str = "latin",
    grouping: str = "auto",
) -> Money | RegionalMiss:
    """Read one regional price: detect the currency, then read the amount.

    Total on ``text`` and ``url``: never raises for them, whatever their type
    or content. Raises only for malformed trusted arguments (``declared``, and
    ``digits``/``grouping`` outside the grammar's own sets, through
    :class:`~price_tracker.core.pricegrammar.PriceContext`).
    """
    expected = expected_currencies(url, declared=declared)
    currency = detect_currency(text, expected=expected)
    amount = None
    if isinstance(text, str):
        ctx = PriceContext(currency=currency, digits=digits, grouping=grouping)
        amount = pricegrammar.parse_price_text(text, ctx)
    if amount is None:
        return RegionalMiss("unreadable")
    if currency is not None and expected and currency not in expected:
        return RegionalMiss("currency_unexpected")
    try:
        return Money(amount, currency)
    except ValueError:
        return RegionalMiss("unreadable")
