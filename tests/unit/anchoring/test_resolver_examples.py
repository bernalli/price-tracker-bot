"""Example tests of price ownership, covering the boundary cases in this module.

Each negative case asserts the exact state and reason and has a positive control on the
same path, so that ``price is None`` cannot pass because of an unrelated failure.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from price_tracker.core.anchoring import (
    AnchorResult,
    AnchorState,
    Money,
    Observation,
    Ownership,
    resolve_anchor,
)
from price_tracker.core.identity import RequestedIdentity
from price_tracker.core.pricegrammar import parse_price_text
from price_tracker.core.structured_data import anchor_structured_page, jsonld_observations

from ._docgen import (
    FINANCING,
    ITEMLIST,
    NONE,
    REQUESTED_URL,
    NodeSpec,
    PageSpec,
    render,
)

REQUESTED = RequestedIdentity.from_url(REQUESTED_URL)
EUR = "EUR"


def obs(
    ownership: Ownership,
    amount: str | None,
    *,
    ref: str,
    key: str | None = None,
    currency: str | None = EUR,
    source: str = "jsonld",
    reason: str = "malformed",
) -> Observation:
    if amount is None:
        return Observation(source, ownership, ref, key, None, reason)
    return Observation(source, ownership, ref, key, Money(Decimal(amount), currency))


def found(result: AnchorResult) -> Decimal:
    assert result.state is AnchorState.FOUND, result
    assert result.price is not None
    return result.price.amount


# ------------------------------------------------------------ worked examples


def test_single_foreign_node_is_ambiguous() -> None:
    foreign = obs(Ownership.FOREIGN, "89.99", ref="n1", key="id:https://x.example.com/b")
    result = resolve_anchor((foreign,))
    assert (result.state, result.reason, result.price) == (
        AnchorState.AMBIGUOUS,
        "foreign_only",
        None,
    )
    # control: the requested node on the same page decides
    mine = obs(Ownership.REQUESTED, "99.99", ref="n0", key="id:" + REQUESTED_URL)
    assert found(resolve_anchor((foreign, mine))) == Decimal("99.99")


def test_foreign_presence_blocks_unknown_fallback() -> None:
    """A foreign node plus an unidentified one is still foreign-only."""
    foreign = obs(Ownership.FOREIGN, "89.99", ref="n1", key="id:https://x.example.com/b")
    unknown = obs(Ownership.UNKNOWN, "99.99", ref="n2")
    unscoped = obs(Ownership.UNSCOPED, "99.99", ref="attr:0", source="attr:data-price")
    result = resolve_anchor((foreign, unknown, unscoped))
    assert (result.state, result.reason) == (AnchorState.AMBIGUOUS, "foreign_only")


def test_two_unknown_nodes_equal_price_is_ambiguous() -> None:
    first = obs(Ownership.UNKNOWN, "50.00", ref="jsonld:0")
    second = obs(Ownership.UNKNOWN, "50.00", ref="jsonld:1")
    result = resolve_anchor((first, second))
    assert (result.state, result.reason, result.price) == (
        AnchorState.AMBIGUOUS,
        "multiple_unknown_nodes",
        None,
    )
    # control: one unidentified node is the page's product
    assert found(resolve_anchor((first,))) == Decimal("50.00")


def test_unscoped_only_is_no_candidate() -> None:
    unscoped = obs(Ownership.UNSCOPED, "29.99", ref="attr:0", source="attr:data-price")
    result = resolve_anchor((unscoped,))
    assert (result.state, result.reason, result.price) == (
        AnchorState.NO_CANDIDATE,
        "unscoped_only",
        None,
    )
    assert result.deciding == (unscoped,)  # kept for the diagnostic


def test_requested_unreadable_never_substituted() -> None:
    """Financing-only requested product, second Product priced."""
    page = PageSpec(
        nodes=(
            NodeSpec(0, Decimal("19.99"), requested=True, price_mode=FINANCING),
            NodeSpec(1, Decimal("449.00"), requested=False, identity=NONE),
        )
    )
    result = anchor_structured_page(render(page).payloads, REQUESTED)
    assert (result.state, result.reason, result.price) == (
        AnchorState.NO_CANDIDATE,
        "requested_unreadable",
        None,
    )
    readable = PageSpec(nodes=(NodeSpec(0, Decimal("499.00"), requested=True), page.nodes[1]))
    assert found(anchor_structured_page(render(readable).payloads, REQUESTED)) == Decimal("499.00")


def _two_products(requested_has_url: bool) -> list[str]:
    foreign: dict[str, object] = {"@context": "https://schema.org", "@type": "Product"}
    foreign["name"] = "Widget"
    foreign["offers"] = {"@type": "Offer", "price": "89.99", "priceCurrency": "EUR"}
    mine: dict[str, object] = {"@context": "https://schema.org", "@type": "Product"}
    mine["name"] = "Widget"
    if requested_has_url:
        mine["url"] = REQUESTED_URL
    mine["offers"] = {"@type": "Offer", "price": "99.99", "priceCurrency": "EUR"}
    return [json.dumps(foreign), json.dumps(mine)]


def test_generic_two_products_no_identity_yields_no_price() -> None:
    result = anchor_structured_page(_two_products(requested_has_url=False), REQUESTED)
    assert (result.state, result.reason, result.price) == (
        AnchorState.AMBIGUOUS,
        "multiple_unknown_nodes",
        None,
    )
    control = anchor_structured_page(_two_products(requested_has_url=True), REQUESTED)
    assert found(control) == Decimal("99.99")
    assert control.reason == "owned"


# ------------------------------------------------------------ node identity and dedup


def test_identity_is_never_taken_from_the_final_url() -> None:
    """A redirect /p/A -> /p/B and a page fully consistent with B is not a read of B."""
    page_b = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "Product",
            "url": "https://shop.example.com/p/widget-b",
            "offers": {"@type": "Offer", "price": "59.00", "priceCurrency": "EUR"},
        }
    )
    result = anchor_structured_page([page_b], REQUESTED)
    assert (result.state, result.reason) == (AnchorState.AMBIGUOUS, "foreign_only")
    as_if_b = RequestedIdentity.from_url("https://shop.example.com/p/widget-b")
    assert found(anchor_structured_page([page_b], as_if_b)) == Decimal("59.00")


def test_homonymous_products_are_two_nodes() -> None:
    """Two different products with the same name are never deduplicated."""
    for second_price in ("40.00", "50.00"):
        a = obs(Ownership.UNKNOWN, "50.00", ref="jsonld:0/0")
        b = obs(Ownership.UNKNOWN, second_price, ref="jsonld:0/1")
        result = resolve_anchor((a, b))
        assert (result.state, result.reason) == (AnchorState.AMBIGUOUS, "multiple_unknown_nodes")
    # at document level: same name, no identifiers
    page = PageSpec(
        nodes=(
            NodeSpec(0, Decimal("50.00"), requested=True, identity=NONE),
            NodeSpec(1, Decimal("50.00"), requested=False, identity=NONE),
        )
    )
    result = anchor_structured_page(render(page).payloads, REQUESTED)
    assert (result.state, result.reason) == (AnchorState.AMBIGUOUS, "multiple_unknown_nodes")


def test_same_node_key_across_sources_is_one_node() -> None:
    """Deduplication is by node key: JSON-LD and microdata of one SKU agree."""
    a = obs(Ownership.UNKNOWN, "50.00", ref="jsonld:0", key="sku:SKU-1")
    b = obs(
        Ownership.UNKNOWN, "50.00", ref="microdata:/html/body/div", key="sku:SKU-1", currency=None
    )
    result = resolve_anchor((a, b))
    assert found(result) == Decimal("50.00")
    assert result.price == Money(Decimal("50.00"), EUR)
    c = obs(Ownership.UNKNOWN, "55.00", ref="microdata:/html/body/div", key="sku:SKU-1")
    disagree = resolve_anchor((a, c))
    assert (disagree.state, disagree.reason) == (AnchorState.AMBIGUOUS, "unknown_node_disagrees")


def test_duplicated_observation_without_key_is_one_node() -> None:
    """Emitting the same unidentified observation twice does not create a node."""
    single = obs(Ownership.UNKNOWN, "50.00", ref="jsonld:0")
    assert resolve_anchor((single,)) == resolve_anchor((single, single, single))
    assert found(resolve_anchor((single, single))) == Decimal("50.00")


def test_page_meta_disagreeing_with_requested_is_ambiguous() -> None:
    mine = obs(Ownership.REQUESTED, "99.99", ref="n0", key="id:" + REQUESTED_URL)
    meta_bad = obs(Ownership.PAGE, "79.99", ref="meta:og", source="og")
    meta_ok = obs(Ownership.PAGE, "99.99", ref="meta:og", source="og")
    result = resolve_anchor((mine, meta_bad))
    assert (result.state, result.reason) == (AnchorState.AMBIGUOUS, "owned_sources_disagree")
    assert found(resolve_anchor((mine, meta_ok))) == Decimal("99.99")


def test_trusted_container_disagreeing_with_requested_is_ambiguous() -> None:
    mine = obs(Ownership.REQUESTED, "99.99", ref="n0", key="id:" + REQUESTED_URL)
    box_bad = obs(Ownership.TRUSTED, "9.99", ref="container:#buybox", source="container")
    box_ok = obs(Ownership.TRUSTED, "99.99", ref="container:#buybox", source="container")
    result = resolve_anchor((mine, box_bad))
    assert (result.state, result.reason) == (AnchorState.AMBIGUOUS, "owned_sources_disagree")
    assert found(resolve_anchor((mine, box_ok))) == Decimal("99.99")


def test_requested_unreadable_is_final_even_with_attributed_price() -> None:
    mine = obs(Ownership.REQUESTED, None, ref="n0", key="id:" + REQUESTED_URL)
    meta = obs(Ownership.PAGE, "79.99", ref="meta:og", source="og")
    result = resolve_anchor((mine, meta))
    assert (result.state, result.reason) == (AnchorState.NO_CANDIDATE, "requested_unreadable")


def test_trusted_container_with_foreign_card() -> None:
    """The buy-box shape: a trusted container and a recommendation card."""
    box = obs(Ownership.TRUSTED, "129.00", ref="container:#buybox", source="container")
    card = obs(Ownership.FOREIGN, "19.99", ref="card:1", key="asin:B000000001")
    assert found(resolve_anchor((box, card))) == Decimal("129.00")
    page = obs(Ownership.PAGE, "119.00", ref="meta:og", source="og")
    result = resolve_anchor((box, card, page))
    assert (result.state, result.reason) == (AnchorState.AMBIGUOUS, "attributed_sources_disagree")


def test_currency_unification() -> None:
    a = obs(Ownership.REQUESTED, "10.00", ref="n0", key="k")
    b = obs(Ownership.PAGE, "10.00", ref="meta", currency=None, source="og")
    assert resolve_anchor((a, b)).price == Money(Decimal("10.00"), EUR)
    c = obs(Ownership.PAGE, "10.00", ref="meta", currency="USD", source="og")
    result = resolve_anchor((a, c))
    assert (result.state, result.reason) == (AnchorState.AMBIGUOUS, "currencies_disagree")
    only_none = obs(Ownership.REQUESTED, "10.00", ref="n0", key="k", currency=None)
    assert resolve_anchor((only_none,)).price == Money(Decimal("10.00"), None)


def test_data_price_attribute_never_decides() -> None:
    """``data-price="2999"`` next to the visible ``29,99``: unscoped, never a price."""
    raw_attr = parse_price_text("2999")
    raw_text = parse_price_text("29,99 €")
    assert raw_attr is not None
    assert raw_text is not None
    attr = Observation("attr:data-price", Ownership.UNSCOPED, "attr:0", None, Money(raw_attr, None))
    text = Observation("css:.price", Ownership.UNSCOPED, "css:0", None, Money(raw_text, EUR))
    result = resolve_anchor((attr, text))
    assert (result.state, result.reason, result.price) == (
        AnchorState.NO_CANDIDATE,
        "unscoped_only",
        None,
    )
    mine = obs(Ownership.REQUESTED, "29.99", ref="n0", key="id:" + REQUESTED_URL)
    assert resolve_anchor((attr, text, mine)).price == Money(Decimal("29.99"), EUR)


def test_nothing_is_no_observation() -> None:
    result = resolve_anchor(())
    assert (result.state, result.reason) == (AnchorState.NO_CANDIDATE, "no_observation")


def test_unreadable_attributed_and_unknown() -> None:
    meta = obs(Ownership.PAGE, None, ref="meta", source="og")
    assert resolve_anchor((meta,)).reason == "attributed_unreadable"
    unknown = obs(Ownership.UNKNOWN, None, ref="n0")
    assert resolve_anchor((unknown,)).reason == "unknown_unreadable"


def test_carousel_member_is_foreign_whatever_its_identity() -> None:
    """A product inside an ItemList is another node, even with the requested URL."""
    for identity in ("url", "none"):
        member = NodeSpec(0, Decimal("24.90"), requested=True, identity=identity, context=ITEMLIST)
        rendered = render(PageSpec(nodes=(member,)))
        result = anchor_structured_page(rendered.payloads, REQUESTED)
        assert (result.state, result.reason, result.price) == (
            AnchorState.AMBIGUOUS,
            "foreign_only",
            None,
        )
    top = NodeSpec(0, Decimal("24.90"), requested=True)  # control: the same node at top level
    assert found(anchor_structured_page(render(PageSpec(nodes=(top,))).payloads, REQUESTED)) == (
        Decimal("24.90")
    )


# ------------------------------------------------------------ foreign-only fixtures


def _fixture_pair() -> tuple[PageSpec, PageSpec, PageSpec]:
    related = NodeSpec(1, Decimal("24.90"), requested=False, context=ITEMLIST)
    positive = PageSpec(
        nodes=(NodeSpec(0, Decimal("89.00"), requested=True), related),
        page_meta=Decimal("89.00"),
    )
    foreign_only = PageSpec(nodes=(related,))  # requested node, its meta: removed entirely
    requested_unreadable = PageSpec(
        nodes=(NodeSpec(0, Decimal("89.00"), requested=True, price_mode=FINANCING), related)
    )
    return positive, foreign_only, requested_unreadable


def test_foreign_only_fixture_has_no_owned_observations() -> None:
    """The foreign-only companion carries no requested, trusted or page observation."""
    positive, foreign_only, requested_unreadable = _fixture_pair()
    rendered = render(foreign_only)
    extracted = jsonld_observations(rendered.payloads, REQUESTED)
    assert isinstance(extracted, tuple)
    everything = extracted + rendered.extra
    owned = {Ownership.REQUESTED, Ownership.TRUSTED, Ownership.PAGE}
    assert [o for o in everything if o.ownership in owned] == []
    assert any(o.ownership is Ownership.FOREIGN and o.price is not None for o in everything)
    literal = Decimal("24.90")
    assert any(o.price is not None and o.price.amount == literal for o in everything)

    result = anchor_structured_page(rendered.payloads, REQUESTED, extra=rendered.extra)
    assert (result.state, result.reason, result.price) == (
        AnchorState.AMBIGUOUS,
        "foreign_only",
        None,
    )
    unreadable = render(requested_unreadable)
    result = anchor_structured_page(unreadable.payloads, REQUESTED, extra=unreadable.extra)
    assert (result.state, result.reason) == (AnchorState.NO_CANDIDATE, "requested_unreadable")
    good = render(positive)
    assert found(anchor_structured_page(good.payloads, REQUESTED, extra=good.extra)) == Decimal(
        "89.00"
    )


# ------------------------------------------------------------ contradictory identity fields


def _n1_payload(order: str) -> str:
    offers = '"offers":{"price":"89.99","priceCurrency":"EUR"}'
    context = '"@context":"https://schema.org","@type":"Product"'
    urls = {
        "BA": '"url":"/p/B","url":"/p/A"',
        "AB": '"url":"/p/A","url":"/p/B"',
        "AA": '"url":"/p/A","url":"/p/A"',
    }[order]
    return "{" + context + "," + urls + "," + offers + "}"


@pytest.mark.parametrize("order", ["BA", "AB", "AA"])
def test_duplicate_identity_key_rejects_the_payload(order: str) -> None:
    """Duplicate identity key: requested ``/p/A``; neither key order may decide ownership."""
    requested = RequestedIdentity.from_url("https://shop.example.com/p/A")
    rescue = Observation(
        "container:#buybox",
        Ownership.TRUSTED,
        "container:#buybox",
        None,
        Money(Decimal("89.99"), EUR),
    )
    result = anchor_structured_page([_n1_payload(order)], requested, extra=(rescue,))
    assert (result.state, result.reason, result.price) == (
        AnchorState.AMBIGUOUS,
        "malformed_structure",
        None,
    )
    unique = (
        '{"@context":"https://schema.org","@type":"Product","url":"/p/A",'
        '"offers":{"price":"89.99","priceCurrency":"EUR"}}'
    )
    assert found(anchor_structured_page([unique], requested)) == Decimal("89.99")


def test_contradictory_identity_fields_in_one_node() -> None:
    payload = json.dumps(
        {
            "@type": "Product",
            "url": REQUESTED_URL,
            "@id": "https://shop.example.com/p/other",
            "offers": {"price": "10", "priceCurrency": "EUR"},
        }
    )
    result = anchor_structured_page([payload], REQUESTED)
    assert (result.state, result.reason) == (AnchorState.AMBIGUOUS, "contradictory_identity")
    consistent = json.dumps(
        {
            "@type": "Product",
            "url": REQUESTED_URL,
            "@id": REQUESTED_URL,
            "offers": {"price": "10", "priceCurrency": "EUR"},
        }
    )
    assert found(anchor_structured_page([consistent], REQUESTED)) == Decimal("10")


def test_one_node_described_with_two_identities_is_contradictory() -> None:
    a = obs(Ownership.REQUESTED, "10", ref="jsonld:0", key="id:" + REQUESTED_URL)
    b = obs(Ownership.FOREIGN, "10", ref="jsonld:0", key="id:https://x.example.com/b")
    result = resolve_anchor((a, b))
    assert (result.state, result.reason) == (AnchorState.AMBIGUOUS, "contradictory_identity")


# ------------------------------------------------------------ unknown veto on rule 2


def test_page_meta_vetoed_by_disagreeing_unknown_node() -> None:
    """A1: a readable unidentified node rejects a disagreeing page-level price."""
    page = obs(Ownership.PAGE, "19.99", ref="meta:og", source="og")
    unknown = obs(Ownership.UNKNOWN, "24.99", ref="jsonld:0")
    result = resolve_anchor((page, unknown))
    assert (result.state, result.reason, result.price) == (
        AnchorState.AMBIGUOUS,
        "attributed_unknown_disagree",
        None,
    )


def test_page_meta_agreeing_with_unknown_node_is_found() -> None:
    """A2: an unidentified node that agrees does not join the deciding set."""
    page = obs(Ownership.PAGE, "19.99", ref="meta:og", source="og")
    unknown = obs(Ownership.UNKNOWN, "19.99", ref="jsonld:0")
    result = resolve_anchor((page, unknown))
    assert (result.state, result.reason) == (AnchorState.FOUND, "attributed")
    assert found(result) == Decimal("19.99")
    assert result.deciding == (page,)


def test_unreadable_unknown_node_never_vetoes() -> None:
    """A3: an unidentified node with no readable price cannot veto anything."""
    page = obs(Ownership.PAGE, "19.99", ref="meta:og", source="og")
    unreadable = obs(Ownership.UNKNOWN, None, ref="jsonld:0")
    result = resolve_anchor((page, unreadable))
    assert (result.state, result.reason) == (AnchorState.FOUND, "attributed")
    assert found(result) == Decimal("19.99")


def test_page_meta_vetoed_by_disagreeing_unknown_currency() -> None:
    """A4: a currency disagreement vetoes even when the amount matches."""
    page = obs(Ownership.PAGE, "19.99", ref="meta:og", source="og")
    unknown = obs(Ownership.UNKNOWN, "19.99", ref="jsonld:0", currency="USD")
    result = resolve_anchor((page, unknown))
    assert (result.state, result.reason, result.price) == (
        AnchorState.AMBIGUOUS,
        "currencies_disagree",
        None,
    )


def test_page_meta_with_no_currency_vetoed_by_unknown_never_gains_one() -> None:
    """A5: an unidentified node never lends its currency to the attributed price."""
    page = obs(Ownership.PAGE, "19.99", ref="meta:og", source="og", currency=None)
    unknown = obs(Ownership.UNKNOWN, "19.99", ref="jsonld:0")
    result = resolve_anchor((page, unknown))
    assert (result.state, result.reason) == (AnchorState.FOUND, "attributed")
    assert result.price == Money(Decimal("19.99"), None)


def test_page_meta_vetoed_by_either_of_two_disagreeing_unknown_nodes() -> None:
    """A6: two unidentified nodes with different node keys, one disagreeing, vetoes."""
    page = obs(Ownership.PAGE, "19.99", ref="meta:og", source="og")
    a = obs(Ownership.UNKNOWN, "19.99", ref="jsonld:0", key="sku:A")
    b = obs(Ownership.UNKNOWN, "24.99", ref="jsonld:1", key="sku:B")
    result = resolve_anchor((page, a, b))
    assert (result.state, result.reason) == (AnchorState.AMBIGUOUS, "attributed_unknown_disagree")
    both_agree = obs(Ownership.UNKNOWN, "19.99", ref="jsonld:1", key="sku:B")
    agreeing_result = resolve_anchor((page, a, both_agree))
    assert (agreeing_result.state, agreeing_result.reason) == (AnchorState.FOUND, "attributed")
    assert found(agreeing_result) == Decimal("19.99")


def test_trusted_container_vetoed_by_disagreeing_unknown_node() -> None:
    """A7: the veto applies identically when the attributed source is TRUSTED."""
    trusted = obs(Ownership.TRUSTED, "19.99", ref="container:#buybox", source="container")
    unknown = obs(Ownership.UNKNOWN, "24.99", ref="jsonld:0")
    result = resolve_anchor((trusted, unknown))
    assert (result.state, result.reason, result.price) == (
        AnchorState.AMBIGUOUS,
        "attributed_unknown_disagree",
        None,
    )


def test_requested_node_ignores_disagreeing_unknown_node() -> None:
    """A8: rule 1 (owned) never looks at unknown nodes."""
    requested = obs(Ownership.REQUESTED, "19.99", ref="n0", key="id:" + REQUESTED_URL)
    unknown = obs(Ownership.UNKNOWN, "24.99", ref="jsonld:0")
    result = resolve_anchor((requested, unknown))
    assert (result.state, result.reason) == (AnchorState.FOUND, "owned")
    assert found(result) == Decimal("19.99")


def test_integration_unidentified_product_node_vetoes_page_meta() -> None:
    """B: full pipeline — a single unidentified Product node vetoes a disagreeing og:price."""
    disagree = PageSpec(
        nodes=(NodeSpec(0, Decimal("24.99"), requested=False, identity=NONE),),
        page_meta=Decimal("19.99"),
    )
    rendered = render(disagree)
    result = anchor_structured_page(rendered.payloads, REQUESTED, extra=rendered.extra)
    assert (result.state, result.reason, result.price) == (
        AnchorState.AMBIGUOUS,
        "attributed_unknown_disagree",
        None,
    )
    # control: same amounts, same shape — the pipeline still finds the attributed price
    agree = PageSpec(
        nodes=(NodeSpec(0, Decimal("19.99"), requested=False, identity=NONE),),
        page_meta=Decimal("19.99"),
    )
    rendered_agree = render(agree)
    control = anchor_structured_page(rendered_agree.payloads, REQUESTED, extra=rendered_agree.extra)
    assert (control.state, control.reason) == (AnchorState.FOUND, "attributed")
    assert found(control) == Decimal("19.99")


def test_unknown_veto_is_permutation_and_duplication_invariant() -> None:
    """A9: the veto outcome does not depend on order or duplication."""
    page = obs(Ownership.PAGE, "19.99", ref="meta:og", source="og")
    unknown = obs(Ownership.UNKNOWN, "24.99", ref="jsonld:0")
    baseline = resolve_anchor((page, unknown))
    assert resolve_anchor((unknown, page)) == baseline
    assert resolve_anchor((page, unknown, unknown, page)) == baseline

    a = obs(Ownership.UNKNOWN, "19.99", ref="jsonld:0", key="sku:A")
    b = obs(Ownership.UNKNOWN, "24.99", ref="jsonld:1", key="sku:B")
    grouped_baseline = resolve_anchor((page, a, b))
    assert resolve_anchor((b, a, page)) == grouped_baseline
    assert resolve_anchor((a, page, b, a, b, page)) == grouped_baseline


def _position_product(**fields: object) -> dict[str, object]:
    return {
        "@type": "Product",
        "offers": {"price": "9.99", "priceCurrency": "EUR"},
        **fields,
    }


def _position_anchor(*documents: object) -> AnchorResult:
    return anchor_structured_page([json.dumps(doc) for doc in documents], REQUESTED)


@pytest.mark.parametrize(
    "document",
    [
        {"@type": "ListItem", "item": _position_product()},
        {"@type": "CollectionPage", "mainEntity": [_position_product()]},
        {"@type": "WebPage", "relatedLink": [_position_product()]},
    ],
    ids=["listitem", "collection_main_array", "related_links"],
)
def test_nested_product_positions_are_foreign(document):
    result = _position_anchor(document)
    assert (result.state, result.reason, result.price) == (
        AnchorState.AMBIGUOUS,
        "foreign_only",
        None,
    )


def test_main_entity_mentions_does_not_inherit_page():
    document = {
        "@type": "WebPage",
        "mainEntity": {"@type": "CreativeWork", "mentions": _position_product()},
    }
    result = _position_anchor(document)
    assert (result.state, result.reason, result.price) == (
        AnchorState.AMBIGUOUS,
        "foreign_only",
        None,
    )


def test_main_entity_of_non_top_level_page_is_foreign():
    """MAIN requires the page granting ``mainEntity`` to be top-level itself: a WebPage
    reached through a non-granting edge (``mentions``) cannot pass PAGE ownership down
    to its own ``mainEntity``, even though the nested node is itself a page type."""
    requested = RequestedIdentity.from_url("https://shop.example.com/p/A")
    nested = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "WebPage",
            "mentions": {
                "@type": "WebPage",
                "mainEntity": {
                    "@type": "Product",
                    "offers": {"@type": "Offer", "price": "9.99", "priceCurrency": "EUR"},
                },
            },
        }
    )
    result = anchor_structured_page([nested], requested)
    assert (result.state, result.reason, result.price) == (
        AnchorState.AMBIGUOUS,
        "foreign_only",
        None,
    )
    # control: the same WebPage at top level, mainEntity direct to the same Product
    top_level = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "WebPage",
            "mainEntity": {
                "@type": "Product",
                "offers": {"@type": "Offer", "price": "9.99", "priceCurrency": "EUR"},
            },
        }
    )
    control = anchor_structured_page([top_level], requested)
    assert (control.state, control.reason) == (AnchorState.FOUND, "attributed")
    assert found(control) == Decimal("9.99")


def test_unresolved_graph_main_entity_is_terminal():
    document = {
        "@graph": [
            {"@type": "WebPage", "mainEntity": {"@id": "#primary"}},
            _position_product(**{"@id": "#recommendation"}),
        ]
    }
    result = _position_anchor(document)
    assert (result.state, result.reason, result.price) == (
        AnchorState.AMBIGUOUS,
        "unresolved_main_entity",
        None,
    )
    # Neither an identified Product nor a trusted source can rescue the level.
    extra = (obs(Ownership.TRUSTED, "9.99", ref="trusted"),)
    rescued = anchor_structured_page(
        [json.dumps(document), json.dumps(_position_product(url=REQUESTED_URL))],
        REQUESTED,
        extra=extra,
    )
    assert rescued == result


@pytest.mark.parametrize("as_array", [False, True])
def test_direct_main_entity_without_identity_is_attributed(as_array):
    product = _position_product()
    value = [product] if as_array else product
    result = _position_anchor({"@type": "WebPage", "mainEntity": value})
    assert result.state is AnchorState.FOUND
    assert result.reason == "attributed"
    assert result.price == Money(Decimal("9.99"), "EUR")


@pytest.mark.parametrize("as_string", [False, True])
@pytest.mark.parametrize("layout", ["graph", "scripts", "root_array"])
def test_main_entity_reference_to_top_product_is_attributed(as_string, layout):
    ref = "#primary" if as_string else {"@id": "#primary"}
    page = {"@type": "WebPage", "mainEntity": ref}
    product = _position_product(**{"@id": REQUESTED_URL + "#primary"})
    for elements in ([page, product], [product, page]):
        documents = (
            [{"@graph": elements}]
            if layout == "graph"
            else ([elements] if layout == "root_array" else elements)
        )
        result = _position_anchor(*documents)
        assert (result.state, result.reason, result.price) == (
            AnchorState.FOUND,
            "attributed",
            Money(Decimal("9.99"), "EUR"),
        )
        assert len(result.deciding) == 1


def test_top_level_unidentified_product_is_singleton():
    result = _position_anchor(_position_product())
    assert (result.state, result.reason, result.price) == (
        AnchorState.FOUND,
        "singleton_unknown",
        Money(Decimal("9.99"), "EUR"),
    )


@pytest.mark.parametrize("target", ["non_product", "duplicates", "nested"])
def test_main_entity_reference_requires_one_top_level_product(target):
    product = _position_product(**{"@id": "#primary"})
    page = {"@type": "WebPage", "mainEntity": "#primary"}
    targets: dict[str, list[dict[str, object]]] = {
        "non_product": [{"@type": "CreativeWork", "@id": "#primary"}],
        "duplicates": [product, product],
        "nested": [{"mentions": product}],
    }
    for documents in ([page, *targets[target]], [*targets[target], page]):
        result = _position_anchor(*documents, _position_product(url=REQUESTED_URL))
        assert (result.state, result.reason, result.price) == (
            AnchorState.AMBIGUOUS,
            "unresolved_main_entity",
            None,
        )


@pytest.mark.parametrize(
    "page_type",
    [
        "CollectionPage",
        "SearchResultsPage",
        ["WebPage", "CustomCollection"],
        ["WebPage", "CustomList"],
        ["WebPage", "CustomSearchResults"],
    ],
)
def test_list_page_main_entity_is_foreign_even_with_requested_url(page_type):
    result = _position_anchor(
        {
            "@type": page_type,
            "mainEntity": _position_product(url=REQUESTED_URL),
        }
    )
    assert result.reason == "foreign_only"
    assert result.price is None


def test_plural_main_entity_is_a_list():
    result = _position_anchor(
        {
            "@type": "WebPage",
            "mainEntity": [_position_product(url=REQUESTED_URL), {}],
        }
    )
    assert result.reason == "foreign_only"
    assert result.price is None


@pytest.mark.parametrize(
    "page_type",
    [
        "ItemPage",
        "ProductPage",
        "AboutPage",
        "CheckoutPage",
        "ContactPage",
        "FAQPage",
        "MedicalWebPage",
        "ProfilePage",
        "QAPage",
        "https://schema.org/ItemPage",
    ],
)
def test_non_list_page_subtypes_allow_direct_main_entity(page_type):
    result = _position_anchor({"@type": page_type, "mainEntity": _position_product()})
    assert (result.state, result.reason, result.price) == (
        AnchorState.FOUND,
        "attributed",
        Money(Decimal("9.99"), "EUR"),
    )
