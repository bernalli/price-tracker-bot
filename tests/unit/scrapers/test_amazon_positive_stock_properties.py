"""Stock evidence must reject negated, future and incomplete statements."""

import string

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


@given(statement=st.sampled_from(("In", "Only 3 left in", "Disponibilità: solo")))
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
