"""Verifies price_tracker.bot.ui.cards.product_card against the seven variants."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from price_tracker.bot.callbacks import InvalidCallback, decode
from price_tracker.bot.messages import set_locale
from price_tracker.bot.ui.cards import product_card
from price_tracker.core.textlimits import SAFE_LIMIT, _is_valid_telegram_markup, visible_length
from price_tracker.i18n.locales import SUPPORTED_LOCALES
from tests.support.card_variants import NOW, VARIANTS, actions_for
from tests.ui.conftest import hostile_text

HALF_WIDTH = 17
ROW_WIDTH = 34


@pytest.fixture(autouse=True)
def _english_locale():
    set_locale("en")
    yield
    set_locale("en")


def _card(name: str):
    view = VARIANTS[name]
    return product_card(view, actions_for(view), now=NOW)


def test_name_row_wraps_the_truncated_escaped_name_in_bidi_isolates() -> None:
    screen = _card("base")
    first_line = screen.text.split("\n")[0]
    assert first_line.startswith("📦 <b>⁨")
    assert first_line.endswith("⁩</b>")


def test_hostile_name_never_leaks_raw_markup() -> None:
    screen = _card("hostile")
    assert screen.text.count("<b>") == 1
    assert screen.text.count("</b>") == 1
    assert "<script" not in screen.text


def test_domain_row_wraps_the_domain_in_bidi_isolates() -> None:
    screen = _card("base")
    domain_line = screen.text.split("\n")[1]
    assert domain_line.startswith("⁨shop.example.com⁩ ")


def test_empty_domain_renders_no_isolate() -> None:
    view = VARIANTS["base"]
    from price_tracker.app.views import ProductView

    no_domain_view = ProductView(
        **{**{f: getattr(view, f) for f in view.__dataclass_fields__}, "domain": ""}
    )
    screen = product_card(no_domain_view, actions_for(no_domain_view), now=NOW)
    assert screen.text.split("\n")[1].startswith(" · #42")


def test_now_naive_rejected() -> None:
    view = VARIANTS["base"]
    with pytest.raises(ValueError, match=r"."):
        product_card(view, actions_for(view), now=datetime(2026, 3, 1, 12, 0))


def test_empty_url_has_no_open_button() -> None:
    screen = _card("hostile")
    callbacks_and_urls = [(btn.callback, btn.url) for row in screen.rows for btn in row]
    assert all(url is None for _callback, url in callbacks_and_urls)


@pytest.mark.parametrize("name", ["paused", "hostile"])
def test_paused_and_suspended_show_reactivate(name: str) -> None:
    screen = _card(name)
    labels = [btn.label for row in screen.rows for btn in row]
    assert any("Reactivate" in label for label in labels)
    assert not any(label.strip("⁨⁩") == "⏸ Pause" for label in labels)


def test_estimate_same_currency_has_no_approx_row() -> None:
    view = VARIANTS["estimate"]
    same_currency_view_kwargs = {field: getattr(view, field) for field in view.__dataclass_fields__}
    same_currency_view_kwargs["reference_currency"] = view.currency
    from price_tracker.app.views import ProductView

    same_currency_view = ProductView(**same_currency_view_kwargs)
    screen = product_card(same_currency_view, actions_for(same_currency_view), now=NOW)
    now_line = next(line for line in screen.text.split("\n") if line.startswith("💰 Now"))
    assert "≈" not in now_line


def test_errors_singular_for_one() -> None:
    view = VARIANTS["errors"]
    from price_tracker.app.views import ProductView

    one_error_view = ProductView(
        **{
            **{f: getattr(view, f) for f in view.__dataclass_fields__},
            "consecutive_errors": 1,
        }
    )
    screen = product_card(one_error_view, actions_for(one_error_view), now=NOW)
    error_line = next(line for line in screen.text.split("\n") if line.startswith("⚠️"))
    assert "1 failed read ·" in error_line
    assert "reads" not in error_line


def test_just_now_renders_just_now() -> None:
    screen = _card("just_now")
    checks_line = next(line for line in screen.text.split("\n") if line.startswith("🔄"))
    assert checks_line.endswith("just now")


def test_last_checked_at_none_renders_never() -> None:
    screen = _card("hostile")
    checks_line = next(line for line in screen.text.split("\n") if line.startswith("🔄"))
    assert checks_line.endswith("never")


def test_initial_equal_current_has_no_change_suffix() -> None:
    screen = _card("hostile")  # initial == current == 0.5
    start_line = next(line for line in screen.text.split("\n") if line.startswith("📌"))
    assert "·" not in start_line


def test_initial_zero_has_no_change_suffix() -> None:
    view = VARIANTS["base"]
    from price_tracker.app.views import ProductView

    zero_initial_view = ProductView(
        **{
            **{f: getattr(view, f) for f in view.__dataclass_fields__},
            "initial": Decimal("0"),
        }
    )
    screen = product_card(zero_initial_view, actions_for(zero_initial_view), now=NOW)
    start_line = next(line for line in screen.text.split("\n") if line.startswith("📌"))
    assert "·" not in start_line


def test_initial_none_has_no_start_row() -> None:
    view = VARIANTS["base"]
    from price_tracker.app.views import ProductView

    no_initial_view = ProductView(
        **{
            **{f: getattr(view, f) for f in view.__dataclass_fields__},
            "initial": None,
        }
    )
    screen = product_card(no_initial_view, actions_for(no_initial_view), now=NOW)
    assert not any(line.startswith("📌") for line in screen.text.split("\n"))


# --- label budget in every locale ------------------------------------------


def _random_view_kwargs(draw: st.DrawFn) -> dict[str, object]:
    status = draw(st.sampled_from(("active", "paused", "suspended")))
    threshold_type = draw(st.sampled_from(("percentage", "absolute", "target", "any_drop")))
    has_current = draw(st.booleans())
    return {
        "id": draw(st.integers(min_value=1, max_value=9_223_372_036_854_775_807)),
        "name": draw(hostile_text),
        "url": draw(st.one_of(st.just(""), st.just("https://shop.example.com/x"))),
        "domain": draw(hostile_text),
        "currency": "EUR",
        "current": Decimal(draw(st.integers(min_value=0, max_value=99999)))
        if has_current
        else None,
        "initial": Decimal(draw(st.integers(min_value=0, max_value=99999))),
        "lowest": None,
        "target": None,
        "threshold_type": threshold_type,
        "threshold_value": Decimal(draw(st.integers(min_value=0, max_value=100))),
        "status": status,
        "consecutive_errors": draw(st.integers(min_value=0, max_value=999)),
        "check_interval_minutes": None,
        "default_interval_minutes": 360,
        "last_checked_at": None,
        "reference_estimate": None,
        "reference_currency": "EUR",
    }


@st.composite
def _hostile_views(draw: st.DrawFn):
    from price_tracker.app.views import ProductView

    return ProductView(**_random_view_kwargs(draw))  # type: ignore[arg-type]


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(view=_hostile_views(), locale=st.sampled_from((*SUPPORTED_LOCALES, "it_IT")))
def test_labels_within_budget_every_locale(view, locale: str, ui_locales) -> None:
    import wcwidth as wcwidth_module

    def wcwidth_oracle(text: str) -> int:
        import unicodedata

        stripped = "".join(
            c for c in text if unicodedata.category(c) not in ("Cf", "Cc", "Zl", "Zp")
        )
        return wcwidth_module.wcswidth(stripped)

    set_locale(locale)
    screen = product_card(view, actions_for(view), now=NOW)

    for row in screen.rows:
        assert len(row) in (1, 2), row
        for btn in row:
            assert btn.label, row
            token = btn.callback if btn.callback is not None else btn.url
            assert token is not None
            if btn.callback is not None:
                assert len(btn.callback.encode("ascii")) <= 64
                assert not isinstance(decode(btn.callback), InvalidCallback), btn.callback
        if len(row) == 2:
            assert wcwidth_oracle(row[0].label) <= HALF_WIDTH, row
            assert wcwidth_oracle(row[1].label) <= HALF_WIDTH, row
        else:
            assert wcwidth_oracle(row[0].label) <= ROW_WIDTH, row

    assert _is_valid_telegram_markup(screen.text)
    assert visible_length(screen.text) <= SAFE_LIMIT

    # The only literal "<"/">" allowed anywhere in the rendered text are the
    # template's own <b>/</b> tags (trusted source, never user content); a
    # pseudo-locale can triplicate the row and its tags, but every occurrence
    # is still exactly one of these two known-safe strings. Once every such
    # occurrence is stripped, nothing from the hostile name or domain may
    # have leaked a raw markup character.
    residual = screen.text.replace("<b>", "").replace("</b>", "")
    assert "<" not in residual, screen.text
    assert ">" not in residual, screen.text
