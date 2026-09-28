"""Domain-to-currency expectations (D6, D7): ccTLD territory, generic ccTLDs.

A generic ccTLD used as a global brand (``GENERIC_CCTLDS``) or ``.eu`` gives no
expectation at any suffix depth: an assumed currency is worse than none,
because it can make a shared symbol resolve to the wrong currency and reject
a correct price at the cross-check (I13).
"""

from __future__ import annotations

from babel.core import get_global
from babel.numbers import get_territory_currencies
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core import identity
from price_tracker.core.currencies import ISO_CURRENCIES, NO_EXPECTATION_TLDS, expected_currencies

# (url, expected result)
CASES: list[tuple[object, frozenset[str]]] = [
    ("https://shop.example.de/p", frozenset({"EUR"})),
    ("https://shop.example.co.uk/p", frozenset({"GBP"})),
    ("https://shop.example.uk/p", frozenset({"GBP"})),
    ("https://shop.example.com.br/p", frozenset({"BRL"})),
    ("https://shop.example.com.au/p", frozenset({"AUD"})),
    ("https://shop.example.co.jp/p", frozenset({"JPY"})),
    ("https://shop.example.co.za/p", frozenset({"ZAR"})),
    ("https://shop.example.ch/p", frozenset({"CHF"})),
    ("https://shop.example.in/p", frozenset({"INR"})),
    ("https://shop.example.jp/p", frozenset({"JPY"})),
    ("https://shop.example.pa/p", frozenset({"PAB", "USD"})),
    ("https://shop.example.cw/p", frozenset()),
    ("https://shop.example.com/p", frozenset()),
    ("https://shop.example.shop/p", frozenset()),
    ("https://shop.example.net/p", frozenset()),
    ("https://shop.example.io/p", frozenset()),
    ("https://shop.example.co/p", frozenset()),
    ("https://shop.example.me/p", frozenset()),
    ("https://shop.example.tv/p", frozenset()),
    ("https://shop.example.ai/p", frozenset()),
    ("https://shop.example.eu/p", frozenset()),
    ("https://shop.example.com.co/p", frozenset()),
    ("https://shop.example.co.me/p", frozenset()),
    ("https://shop.example.com.ai/p", frozenset()),
    ("https://shop.example.za/p", frozenset()),  # no suffix in the PSL
    ("https://shop.example.bd/p", frozenset({"BDT"})),  # wildcard *.bd: suffix "example.bd"
    ("https://shop.example.xk/p", frozenset()),  # ".xk" is not a TLD
    ("https://shop.example.xn--p1ai/p", frozenset()),
    ("https://shop.example.рф/p", frozenset()),
    ("http://192.0.2.1/p", frozenset()),
    ("https://localhost/p", frozenset()),
    ("https://[::1]/p", frozenset()),
    ("https://EXAMPLE.DE./p", frozenset({"EUR"})),
    ("https://user:pass@shop.example.de/p", frozenset()),
    ("https://shop.example.de/p\n", frozenset()),
    (None, frozenset()),
    (b"https://shop.example.de/p", frozenset()),
    (42, frozenset()),
    ("", frozenset()),
    ("x" * 4097, frozenset()),
    ("ftp://shop.example.de/p", frozenset()),
    ("javascript:alert(1)", frozenset()),
    ("/p", frozenset()),
    ("https://shop.example.de:8443/p", frozenset({"EUR"})),  # positive control with a port
]

# Territories with a currency today but no suffix in the PSL that tldextract 5.3.2 knows,
# excluded from the exhaustive property below. Pinned so a PSL/babel update is a visible
# red instead of a silently narrower property.
_EMPTY_SUFFIX_WITH_CURRENCY = frozenset(
    {"BL", "BQ", "DG", "EA", "EH", "IC", "MF", "TA", "UM", "XK", "ZA"}
)


def test_expected_currencies_table() -> None:
    for url, expected in CASES:
        assert expected_currencies(url) == expected, f"{url!r} -> {expected_currencies(url)!r}"


def test_declared_wins_over_the_domain() -> None:
    assert expected_currencies("https://shop.example.de/p", declared=frozenset({"CHF"})) == (
        frozenset({"CHF"})
    )


@given(text=st.text(max_size=200))
@settings(max_examples=300, deadline=None)
def test_totality_on_arbitrary_text(text: str) -> None:
    result = expected_currencies(text)
    assert isinstance(result, frozenset)
    assert result <= ISO_CURRENCIES


@given(url=st.from_regex(r"https?://[a-z0-9.-]{1,60}/[a-z0-9/?=&_-]{0,40}", fullmatch=True))
@settings(max_examples=300, deadline=None)
def test_totality_on_constructed_urls(url: str) -> None:
    result = expected_currencies(url)
    assert isinstance(result, frozenset)
    assert result <= ISO_CURRENCIES


def test_every_territory_ccTLD_matches_its_currencies() -> None:
    checked = 0
    for tt in get_global("territory_currencies"):
        if len(tt) != 2 or not tt.isalpha():
            continue
        if tt.lower() in NO_EXPECTATION_TLDS:
            continue
        host = f"shop.example.{tt.lower()}"
        if not identity._extractor(host).suffix:
            continue
        checked += 1
        expected = frozenset(get_territory_currencies(tt)) & ISO_CURRENCIES
        got = expected_currencies(f"https://{host}/p")
        if tt.upper() == "UK":
            assert got == frozenset({"GBP"})
        else:
            assert got == expected, f"{tt}: got {got!r}, expected {expected!r}"
    assert checked > 0


def test_empty_suffix_territories_with_a_currency_are_pinned() -> None:
    """Territories whose currency is non-empty but whose ccTLD has no PSL suffix.

    Excluded from the exhaustive property above for construction, not by chance:
    pinned here so a tldextract/babel update that changes the set is a visible red,
    naming both versions, instead of a silently narrower property.
    """
    found = set()
    for tt in get_global("territory_currencies"):
        if len(tt) != 2 or not tt.isalpha():
            continue
        if tt.lower() in NO_EXPECTATION_TLDS:
            continue
        host = f"shop.example.{tt.lower()}"
        if identity._extractor(host).suffix:
            continue
        if get_territory_currencies(tt):
            found.add(tt)
    assert found == set(_EMPTY_SUFFIX_WITH_CURRENCY), (
        f"babel/tldextract changed: got {sorted(found)}, "
        f"pinned {sorted(_EMPTY_SUFFIX_WITH_CURRENCY)}"
    )
