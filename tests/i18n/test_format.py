"""Verifies price_tracker.i18n.format: pinned Babel output, rounding, not-well-formed input."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
from babel.numbers import get_currency_precision, list_currencies
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.i18n.format import ago, change, delta, duration, money, percent, when
from price_tracker.i18n.locales import SUPPORTED_LOCALES

CURRENCIES = tuple(sorted(list_currencies()))


# --- money: pinned currency examples (P10) ---------------------------------


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("en", "€1,299.99"),
        ("it", "1.299,99\xa0€"),
        ("es", "1.299,99\xa0€"),
        ("de", "1.299,99\xa0€"),
        ("fr", "1 299,99\xa0€"),
        ("uk", "1\xa0299,99\xa0EUR"),
        ("pt_BR", "€\xa01.299,99"),
        ("zh_Hans", "€1,299.99"),
        ("ja", "€1,299.99"),
    ],
)
def test_money_eur_pinned_examples(locale: str, expected: str) -> None:
    assert money(Decimal("1299.99"), "EUR", locale=locale) == expected


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("en", "¥12,800"),
        ("it", "12.800\xa0JPY"),
        ("de", "12.800\xa0¥"),
        ("ja", "￥12,800"),
        ("zh_Hans", "JP¥12,800"),
    ],
)
def test_money_jpy_pinned_examples(locale: str, expected: str) -> None:
    assert money(Decimal("12800"), "JPY", locale=locale) == expected


def test_money_currency_precision() -> None:
    assert get_currency_precision("JPY") == 0
    assert get_currency_precision("EUR") == 2
    assert get_currency_precision("BHD") == 3


def test_money_half_up_disagrees_with_babel_half_even() -> None:
    # Babel's own decimal-context rounding is half-even: 1299.985 -> 1,299.98.
    # money() must round half-up instead: 1299.985 -> 1,299.99.
    assert money(Decimal("1299.985"), "EUR", locale="en") == "€1,299.99"


def test_money_fr_usd_half_dollar() -> None:
    assert money(Decimal("0.5"), "USD", locale="fr") == "0,50\xa0$US"


def test_money_it_IT_renders_like_it() -> None:  # noqa: N802
    assert money(Decimal("1299.99"), "EUR", locale="it_IT") == money(
        Decimal("1299.99"), "EUR", locale="it"
    )


@pytest.mark.parametrize(
    "bad_amount",
    [
        None,
        Decimal("NaN"),
        Decimal("-Infinity"),
        Decimal("-1"),
        Decimal("1e15"),
        1.0,
        1,
        "1.00",
        Decimal("-0"),
        Decimal("-0.00"),
        True,
    ],
)
def test_money_rejects_not_well_formed_amount(bad_amount: object) -> None:
    with pytest.raises(ValueError, match=r"."):
        money(bad_amount, "EUR", locale="en")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_currency", ["eur", "XYZ", "EU", "EURO", "", None])
def test_money_rejects_not_well_formed_currency(bad_currency: object) -> None:
    with pytest.raises(ValueError, match=r"."):
        money(Decimal("1"), bad_currency, locale="en")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_locale", ["xx_YY", "", None])
def test_money_rejects_not_well_formed_locale(bad_locale: object) -> None:
    with pytest.raises(ValueError, match=r"."):
        money(Decimal("1"), "EUR", locale=bad_locale)  # type: ignore[arg-type]


# --- money: property 6 (§17.3) ----------------------------------------------


def _extract_digits(text: str) -> str:
    """Digits only, group separators and the decimal mark stripped.

    Every supported locale renders amounts with ASCII digits (verified by
    P10, including ja/zh_Hans), and every group separator or decimal mark
    Babel could use for these locales is itself a non-digit character, so
    filtering to ``str.isdigit()`` strips exactly the separators.
    """
    return "".join(char for char in text if char.isdigit())


@settings(max_examples=200, deadline=None)
@given(
    locale=st.sampled_from(SUPPORTED_LOCALES),
    currency=st.sampled_from(CURRENCIES),
    whole=st.integers(min_value=0, max_value=10**9),
    decimal_places=st.integers(min_value=0, max_value=4),
    fraction=st.integers(min_value=0, max_value=9999),
)
def test_money_digits_reconstruct_the_quantized_amount(
    locale: str, currency: str, whole: int, decimal_places: int, fraction: int
) -> None:
    fraction_text = str(fraction).zfill(4)[:decimal_places]
    amount_text = f"{whole}.{fraction_text}" if decimal_places else str(whole)
    amount = Decimal(amount_text)
    formatted = money(amount, currency, locale=locale)

    digits = get_currency_precision(currency)
    quantum = Decimal(1).scaleb(-digits)
    quantized = amount.quantize(quantum, rounding=ROUND_HALF_UP)

    expected_digits = _extract_digits(f"{quantized:f}")
    got_digits = _extract_digits(formatted)
    assert got_digits == expected_digits, (
        f"locale={locale!r} currency={currency!r} amount={amount!r} "
        f"formatted={formatted!r} expected_digits={expected_digits!r} got_digits={got_digits!r}"
    )


# --- delta / change ----------------------------------------------------------


def test_delta_down_glyph() -> None:
    assert delta(Decimal("10"), Decimal("8"), "EUR", locale="en") == "▼ €2.00"


def test_delta_up_glyph() -> None:
    assert delta(Decimal("8"), Decimal("10"), "EUR", locale="en") == "▲ €2.00"


def test_delta_equal_is_empty() -> None:
    assert delta(Decimal("10"), Decimal("10"), "EUR", locale="en") == ""


def test_delta_never_uses_a_locale_minus_sign() -> None:
    text = delta(Decimal("1000"), Decimal("1"), "EUR", locale="de")
    assert "-" not in text
    assert text.startswith("▼")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (Decimal("NaN"), Decimal("1")),
        (Decimal("1"), Decimal("NaN")),
        (Decimal("-Infinity"), Decimal("1")),
        (1.0, Decimal("1")),
        (Decimal("1"), 1.0),
        (Decimal("-0"), Decimal("1")),
    ],
)
def test_delta_rejects_not_well_formed_operands_without_invalid_operation(
    old: object, new: object
) -> None:
    with pytest.raises(ValueError, match=r"."):
        delta(old, new, "EUR", locale="en")  # type: ignore[arg-type]


def test_change_down_glyph() -> None:
    assert change(Decimal("100"), Decimal("90"), locale="en") == "▼ 10%"


def test_change_up_glyph() -> None:
    assert change(Decimal("100"), Decimal("110"), locale="en") == "▲ 10%"


def test_change_equal_is_empty() -> None:
    assert change(Decimal("100"), Decimal("100"), locale="en") == ""


def test_change_zero_initial_is_empty() -> None:
    assert change(Decimal("0"), Decimal("10"), locale="en") == ""


@pytest.mark.parametrize(
    ("initial", "current"),
    [
        (Decimal("NaN"), Decimal("1")),
        (Decimal("1"), Decimal("NaN")),
        (Decimal("-Infinity"), Decimal("1")),
        (1.0, Decimal("1")),
        (Decimal("1"), 1.0),
        (Decimal("-0"), Decimal("1")),
    ],
)
def test_change_rejects_not_well_formed_operands_without_invalid_operation(
    initial: object, current: object
) -> None:
    with pytest.raises(ValueError, match=r"."):
        change(initial, current, locale="en")  # type: ignore[arg-type]


# --- percent -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("en", "13.3%"),
        ("it", "13,3%"),
        ("zh_Hans", "13.3%"),
        ("uk", "13,3%"),
        ("pt_BR", "13,3%"),
        ("ja", "13.3%"),
        ("fr", "13,3\xa0%"),
        ("es", "13,3\xa0%"),
        ("de", "13,3\xa0%"),
    ],
)
def test_percent_pinned_pattern_per_locale(locale: str, expected: str) -> None:
    assert percent(Decimal("0.133"), locale=locale) == expected


def test_percent_zero() -> None:
    assert percent(Decimal("0"), locale="en") == "0%"


def test_percent_one() -> None:
    assert percent(Decimal("1.0"), locale="en") == "100%"


def test_percent_one_fraction_digit() -> None:
    assert percent(Decimal("0.1234"), locale="en") == "12.3%"


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (Decimal("0.1225"), "12.3%"),
        (Decimal("0.1235"), "12.4%"),
        (Decimal("0.0005"), "0.1%"),
    ],
)
def test_percent_rounds_half_up(ratio: Decimal, expected: str) -> None:
    # Half-even would give 12.2%, 12.4%, 0% for these three.
    assert percent(ratio, locale="en") == expected


@pytest.mark.parametrize("bad", [Decimal("-0.1"), Decimal("NaN"), 0.5, 1, Decimal("-0")])
def test_percent_rejects_not_well_formed_ratio(bad: object) -> None:
    with pytest.raises(ValueError, match=r"."):
        percent(bad, locale="en")  # type: ignore[arg-type]


# --- ago ----------------------------------------------------------------------


@pytest.mark.parametrize("seconds", [0, 30, -300])
def test_ago_none_under_one_minute_or_negative(seconds: int) -> None:
    assert ago(timedelta(seconds=seconds), locale="en") is None


def test_ago_exactly_one_minute() -> None:
    assert ago(timedelta(seconds=60), locale="en") == "1 minute ago"


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("en", "6 hours ago"),
        ("it", "6 ore fa"),
        ("zh_Hans", "6小时前"),
        ("fr", "il y a 6 heures"),
        ("es", "hace 6 horas"),
        ("de", "vor 6 Stunden"),
        ("uk", "6 годин тому"),
        ("pt_BR", "há 6 horas"),
        ("ja", "6 時間前"),
    ],
)
def test_ago_six_hours_pinned_examples(locale: str, expected: str) -> None:
    assert ago(timedelta(hours=6), locale=locale) == expected


def test_ago_one_hour_boundary() -> None:
    assert ago(timedelta(seconds=3599), locale="en") == "1 hour ago"


def test_ago_one_day_boundary() -> None:
    assert ago(timedelta(seconds=86399), locale="en") == "1 day ago"


def test_ago_one_month() -> None:
    assert ago(timedelta(days=40), locale="en") == "1 month ago"


def test_ago_one_year() -> None:
    assert ago(timedelta(days=400), locale="en") == "1 year ago"


def test_ago_rejects_none() -> None:
    with pytest.raises((ValueError, TypeError)):
        ago(None, locale="en")  # type: ignore[arg-type]


# --- duration -------------------------------------------------------------


@pytest.mark.parametrize("minutes", [5, 30, 90])
def test_duration_stays_in_minutes_when_not_evenly_divisible(minutes: int) -> None:
    assert duration(minutes, locale="en") == f"{minutes} min"


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("en", "90 min"),
        ("it", "90 min"),
        ("de", "90 Min."),
        ("fr", "90\xa0min"),
        ("ja", "90 分"),
        ("uk", "90 хв"),
    ],
)
def test_duration_ninety_minutes_pinned_examples(locale: str, expected: str) -> None:
    assert duration(90, locale=locale) == expected


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("en", "6 hr"),
        ("it", "6 h"),
        ("de", "6 Std."),
        ("fr", "6 h"),
        ("es", "6 h"),
        ("pt_BR", "6 h"),
    ],
)
def test_duration_six_hours_pinned_examples(locale: str, expected: str) -> None:
    assert duration(360, locale=locale) == expected


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("en", "7 days"),
        ("it", "7 giorni"),
        ("fr", "7 j"),
        ("de", "7 Tg."),
    ],
)
def test_duration_seven_days_pinned_examples(locale: str, expected: str) -> None:
    assert duration(10080, locale=locale) == expected


@pytest.mark.parametrize("minutes", [120, 1440])
def test_duration_evenly_divisible_picks_the_larger_unit(minutes: int) -> None:
    rendered = duration(minutes, locale="en")
    assert "min" not in rendered


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "5"])
def test_duration_rejects_not_well_formed_minutes(bad: object) -> None:
    with pytest.raises(ValueError, match=r"."):
        duration(bad, locale="en")  # type: ignore[arg-type]


# --- when -----------------------------------------------------------------

_WHEN_INSTANT = datetime(2026, 9, 23, 14, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("en", "9/23/26, 4:05 PM"),
        ("it", "23/09/26, 16:05"),
        ("ja", "2026/09/23 16:05"),
        ("de", "23.09.26, 16:05"),
        ("zh_Hans", "2026/9/23 16:05"),
    ],
)
def test_when_pinned_examples(locale: str, expected: str) -> None:
    assert when(_WHEN_INSTANT, tz="Europe/Rome", locale=locale) == expected


@pytest.mark.parametrize("code", [*SUPPORTED_LOCALES, "it_IT"])
def test_when_every_supported_locale_parses(code: str) -> None:
    when(_WHEN_INSTANT, tz="UTC", locale=code)


def test_when_rejects_unknown_timezone() -> None:
    with pytest.raises(ValueError, match=r"."):
        when(_WHEN_INSTANT, tz="Mars/Olympus", locale="en")


@pytest.mark.parametrize("bad_tz", ["Europe", "europe/Rome"])
def test_when_rejects_malformed_timezone(bad_tz: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        when(_WHEN_INSTANT, tz=bad_tz, locale="en")


def test_when_rejects_non_str_timezone() -> None:
    with pytest.raises(TypeError):
        when(_WHEN_INSTANT, tz=42, locale="en")  # type: ignore[arg-type]


def test_when_rejects_naive_instant() -> None:
    with pytest.raises(ValueError, match=r"."):
        when(datetime(2026, 9, 23, 14, 5), tz="UTC", locale="en")


# --- locale validation shared by every function -----------------------------


@pytest.mark.parametrize("bad_locale", ["xx_YY", "", None])
def test_every_function_rejects_not_well_formed_locale(bad_locale: object) -> None:
    with pytest.raises(ValueError, match=r"."):
        percent(Decimal("0.1"), locale=bad_locale)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"."):
        duration(5, locale=bad_locale)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"."):
        when(_WHEN_INSTANT, tz="UTC", locale=bad_locale)  # type: ignore[arg-type]
