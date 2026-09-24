"""Grammar of the free-text inputs accepted by the guided flows.

Per-parser corpora (``tests/support/input_corpus.py``) pin accepted values and
rejections; a property over random Unicode proves every parser is total and
returns either a value inside its declared domain or a known ``InputError``.
"""

from __future__ import annotations

import ipaddress
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.app import inputs
from price_tracker.app.inputs import (
    Absolute,
    AnyDrop,
    Cancel,
    ClearTarget,
    Forever,
    InputError,
    InputErrorCode,
    IntervalMinutes,
    Off,
    Percentage,
    QuietHours,
    ResetInterval,
    SetTarget,
)
from price_tracker.core.url_utils import UnsafeURLError
from tests.support.input_corpus import ACCEPTED, REJECTED

if TYPE_CHECKING:
    from collections.abc import Callable


def _offline_guard(url: str) -> None:
    """SSRF guard without DNS: literal addresses only (the real guard also resolves)."""
    host = urlparse(url).hostname
    urlparse(url).port  # noqa: B018 - raises ValueError on an invalid port, like the real guard
    if host is None:
        raise UnsafeURLError("no host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if address.is_private or address.is_loopback or address.is_link_local:
        raise UnsafeURLError("non-public")


PARSERS: dict[str, Callable[[str], Any]] = {
    "threshold": inputs.parse_threshold,
    "target": inputs.parse_target,
    "product_interval": inputs.parse_product_interval,
    "global_interval": inputs.parse_global_interval,
    "digest_interval": inputs.parse_digest_interval,
    "throttle": inputs.parse_throttle,
    "mute_hours": inputs.parse_mute_hours,
    "user_id": inputs.parse_user_id,
    "nickname": inputs.parse_nickname,
    "currency_code": inputs.parse_currency_code,
    "timezone": inputs.parse_timezone,
    "quiet_hours": inputs.parse_quiet_hours,
    "url": lambda text: inputs.parse_url(text, guard=_offline_guard),
}

_MAX_AMOUNT = Decimal("999999999.9999")


def _amount_ok(amount: Decimal) -> bool:
    exponent = amount.as_tuple().exponent
    return (
        amount.is_finite()
        and Decimal(0) < amount <= _MAX_AMOUNT
        and isinstance(exponent, int)
        and exponent >= -4
    )


def _in_domain(name: str, value: Any) -> bool:
    """Independent domain predicates, from the grammar table (not from the parsers)."""
    checks: dict[str, Callable[[Any], bool]] = {
        "threshold": lambda v: (
            (isinstance(v, Percentage) and 1 <= v.value <= 99)
            or (isinstance(v, Absolute) and _amount_ok(v.amount))
            or isinstance(v, AnyDrop | Cancel)
        ),
        "target": lambda v: (
            (isinstance(v, SetTarget) and _amount_ok(v.amount))
            or isinstance(v, ClearTarget | Cancel)
        ),
        "product_interval": lambda v: (
            (isinstance(v, IntervalMinutes) and 5 <= v.minutes <= 10080)
            or isinstance(v, ResetInterval)
        ),
        "global_interval": lambda v: isinstance(v, int) and 5 <= v <= 10080,
        "digest_interval": lambda v: isinstance(v, int) and 5 <= v <= 1440,
        "throttle": lambda v: (isinstance(v, int) and v >= 1) or isinstance(v, Off),
        "mute_hours": lambda v: (isinstance(v, int) and 1 <= v <= 8760) or isinstance(v, Forever),
        "user_id": lambda v: isinstance(v, int) and 1 <= v <= 2**63 - 1,
        "nickname": lambda v: isinstance(v, str) and len(v) >= 1 and v == v.strip(),
        "currency_code": lambda v: isinstance(v, str) and len(v) == 3 and v.isupper(),
        "timezone": lambda v: isinstance(v, str) and bool(v),
        "quiet_hours": lambda v: (
            (isinstance(v, QuietHours) and v.start != v.end) or isinstance(v, Off)
        ),
        "url": lambda v: isinstance(v, str) and v.lower().startswith(("http://", "https://")),
    }
    return checks[name](value)


def _cases(table: dict[str, list[Any]]) -> list[tuple[str, Any]]:
    return [(name, case) for name, cases in table.items() for case in cases]


@pytest.mark.parametrize(("name", "case"), _cases(ACCEPTED), ids=repr)
def test_accepted_corpus(name: str, case: tuple[str, Any]) -> None:
    text, expected = case
    assert PARSERS[name](text) == expected


@pytest.mark.parametrize(("name", "text"), _cases(REJECTED), ids=repr)
def test_rejected_corpus(name: str, text: str) -> None:
    result = PARSERS[name](text)
    assert isinstance(result, InputError), result
    assert result.code in set(InputErrorCode)


def test_single_separator_is_always_decimal() -> None:
    """1.299 and 1,299 both mean 1.299; locale never changes it."""
    assert (
        inputs.parse_target("1.299") == inputs.parse_target("1,299") == SetTarget(Decimal("1.299"))
    )


def test_error_codes_name_the_reason() -> None:
    assert inputs.parse_threshold("NaN") == InputError(InputErrorCode.NOT_A_NUMBER)
    assert inputs.parse_threshold("999%") == InputError(InputErrorCode.OUT_OF_RANGE)
    assert inputs.parse_threshold("1" * 65) == InputError(InputErrorCode.TOO_LONG)
    assert inputs.parse_threshold("5\u202e") == InputError(InputErrorCode.CONTROL_CHARACTER)
    assert inputs.parse_threshold("  ") == InputError(InputErrorCode.EMPTY)
    assert inputs.parse_user_id("9223372036854775808") == InputError(InputErrorCode.OUT_OF_RANGE)
    assert inputs.parse_url("http://127.0.0.1/x") == InputError(InputErrorCode.UNSAFE_URL)


def test_url_longer_than_the_scalar_limit_is_valid_with_the_real_guard() -> None:
    url = "http://93.184.216.34/" + "a" * 80
    assert len(url) > inputs.SCALAR_MAX_CHARS
    assert inputs.parse_url(url) == url


@settings(max_examples=1500, deadline=None)
@given(name=st.sampled_from(sorted(PARSERS)), text=st.text(max_size=80))
def test_every_parser_is_total_on_random_unicode(name: str, text: str) -> None:
    result = PARSERS[name](text)
    if isinstance(result, InputError):
        assert result.code in set(InputErrorCode)
    else:
        assert _in_domain(name, result), (name, text, result)


@settings(max_examples=1500, deadline=None)
@given(
    name=st.sampled_from(sorted(PARSERS)),
    text=st.text(alphabet="0123456789.,%-+eE \t\u202e\uff15anyoffNaInf:/", max_size=24),
)
def test_every_parser_is_total_on_grammar_shaped_noise(name: str, text: str) -> None:
    result = PARSERS[name](text)
    if isinstance(result, InputError):
        assert result.code in set(InputErrorCode)
    else:
        assert _in_domain(name, result), (name, text, result)


@settings(max_examples=200, deadline=None)
@given(
    left=st.from_regex(r"[1-9][0-9]{0,3}", fullmatch=True),
    right=st.from_regex(r"[0-9]{1,4}", fullmatch=True),
    separator=st.sampled_from((" ", "\u00a0", "_", "..", ",,")),
)
def test_numeric_parsers_reject_internal_separators(left: str, right: str, separator: str) -> None:
    text = left + separator + right
    for name in (
        "threshold",
        "target",
        "product_interval",
        "global_interval",
        "digest_interval",
        "throttle",
        "mute_hours",
        "user_id",
    ):
        assert isinstance(PARSERS[name](text), InputError), (name, text)
