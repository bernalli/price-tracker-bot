"""Stock evidence must reject negated, future and incomplete statements."""

import string

import pytest
from bs4 import BeautifulSoup
from hypothesis import given
from hypothesis import strategies as st

from price_tracker.scrapers import amazon
from tests.unit.scrapers.test_amazon_positive_stock import (
    ASIN,
    CART,
    accordion_page,
    availability,
)


@given(
    fragment=st.text(alphabet=string.ascii_letters + string.digits + " .:-", max_size=32),
    statement=st.sampled_from(("Not in stock", "Will be in stock", "In stock on")),
    copies=st.integers(min_value=1, max_value=3),
    reverse=st.booleans(),
)
def test_unresolved_stock_cannot_be_overridden(fragment, statement, copies, reverse):
    parts = [availability("In stock"), availability(statement + " " + fragment)] * copies
    if reverse:
        parts.reverse()
    soup = BeautifulSoup(accordion_page("".join(parts) + CART), "html.parser")
    price = soup.select_one(".a-offscreen")
    assert price is not None
    assert amazon._offer_in_stock(price, soup, ASIN) is False


@given(
    statement=st.sampled_from(
        ("Disponibilità: solo", "In stock on", "Only 3 left in stock on", "Nur noch 3 auf Lager ab")
    )
)
def test_truncated_stock_statement_is_not_evidence(statement):
    soup = BeautifulSoup(accordion_page(availability(statement) + CART), "html.parser")
    price = soup.select_one(".a-offscreen")
    assert price is not None
    assert amazon._offer_in_stock(price, soup, ASIN) is False


@given(quantity=st.integers(min_value=-10000, max_value=0))
def test_nonpositive_stock_quantity_cannot_confirm_stock(quantity):
    for statement in (
        f"Only {quantity} left in stock",
        f"Disponibilità: solo {quantity}",
    ):
        soup = BeautifulSoup(accordion_page(availability(statement) + CART), "html.parser")
        price = soup.select_one(".a-offscreen")
        assert price is not None
        assert amazon._offer_in_stock(price, soup, ASIN) is False


def offer_in_stock(page: str) -> bool:
    soup = BeautifulSoup(page, "html.parser")
    price = soup.select_one(".a-offscreen")
    assert price is not None
    return amazon._offer_in_stock(price, soup, ASIN)


@pytest.mark.parametrize(
    "statement",
    [
        "In stock",
        "In Stock.",
        "Disponibilità immediata.",
        "Disponibilità: solo 2",
        "Only 3 left in stock - order soon.",
        "Auf Lager.",
        "En stock.",
        "在庫あり",
        "在庫あり。",
        "Only 1 left in stock (more on the way).",
        "Disponibilità: solo 2 - ordina subito.",
        "Nur noch 3 auf Lager",
        "Il ne reste plus que 3 exemplaire(s) en stock.",
    ],
)
@pytest.mark.parametrize("cart", [CART, ""])
def test_real_affirmative_stock_statement_is_evidence(statement, cart):
    assert offer_in_stock(accordion_page(availability(statement) + cart)) is True


@pytest.mark.parametrize(
    "statement",
    [
        "Only 1 left in stock (not in stock yet).",
        "Only 1 left in stock (will be in stock on December 20).",
        "Only 1 left in stock (not available yet).",
        "Only 1 left in stock (more on the way, not available yet).",
        "Disponibilità: solo 2 (non ancora disponibile).",
        "Disponibilità: solo 2 (a partire dal 20 dicembre).",
        "Nur noch 3 auf Lager (ab 20. Dezember).",
        "Only 1 left in stock (unknown status).",
        "Only 1 left in stock ().",
    ],
)
@pytest.mark.parametrize("cart", [CART, ""])
def test_unapproved_parenthetical_stock_suffix_is_not_evidence(statement, cart):
    assert offer_in_stock(accordion_page(availability(statement) + cart)) is False


@pytest.mark.parametrize(
    "statement",
    [
        "Only 1 left in stock (more on the way).",
        "Only 3 left in stock (MORE ON THE WAY).",
        "Only 2 left in stock (more   on the way).",
    ],
)
@pytest.mark.parametrize("cart", [CART, ""])
def test_allowed_parenthetical_stock_suffix_remains_evidence(statement, cart):
    assert offer_in_stock(accordion_page(availability(statement) + cart)) is True


NON_STOCK = "Usually dispatched within 2 to 3 days."


def test_non_stock_statement_leaves_the_decision_to_an_enabled_purchase_control():
    assert offer_in_stock(accordion_page(availability(NON_STOCK) + CART)) is True


@pytest.mark.parametrize(
    "control",
    [
        "",
        '<input id="add-to-cart-button" name="submit.add-to-cart" type="submit" disabled>',
        '<span class="a-button-disabled">' + CART + "</span>",
    ],
)
def test_non_stock_statement_without_an_enabled_purchase_control_is_not_evidence(control):
    assert offer_in_stock(accordion_page(availability(NON_STOCK) + control)) is False
