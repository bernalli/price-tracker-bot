"""Property tests of price ownership.

* (a) **No foreign price, ever.** The generator knows which node is the requested
  product; the oracle is the set of amounts that belong to it by construction
  (:attr:`PageSpec.allowed_amounts`), never a call to the resolver. Model assumption,
  stated because the property depends on it: when the requested product is absent from
  the page, every other product is *observably* foreign (a different identifier, or a
  carousel context) — an unidentified node alone on a page is indistinguishable from
  the page's product, and the specification accepts it as such.
* (b) **Order and duplication invariance** (I22), over observations and documents.
* The exhaustive outcome table of §5.2.6 (full product, not a sample).
* Liveness: an identified, readable requested product is always found, so (a) cannot
  pass by returning nothing.
"""

from __future__ import annotations

import itertools
import json
from decimal import Decimal

from hypothesis import event, given, settings
from hypothesis import strategies as st

from price_tracker.core.anchoring import (
    AnchorResult,
    AnchorState,
    Money,
    Observation,
    Ownership,
    resolve_anchor,
)
from price_tracker.core.identity import RequestedIdentity
from price_tracker.core.structured_data import (
    _IDENTIFIER_FIELDS,
    anchor_structured_page,
    jsonld_observations,
)

from ._docgen import (
    FINANCING,
    FRAGMENT,
    ITEMLIST,
    MAIN_ENTITY,
    MALFORMED,
    MULTI_CURRENCY,
    NO_OFFERS,
    NONE,
    READABLE,
    REQUESTED_URL,
    SKU,
    TOP,
    URL,
    URL_RELATIVE,
    URL_TRACKING,
    NodeSpec,
    PageSpec,
    render,
)

REQUESTED = RequestedIdentity.from_url(REQUESTED_URL)
PRICE_MODES = [READABLE, READABLE, FINANCING, MALFORMED, NO_OFFERS, MULTI_CURRENCY]
REQUESTED_IDENTITIES = [URL, URL_RELATIVE, URL_TRACKING, FRAGMENT, SKU, NONE]
FOREIGN_IDENTITIES = [URL, URL_RELATIVE, FRAGMENT, SKU, NONE]
OBSERVABLY_FOREIGN = [URL, URL_RELATIVE]


@st.composite
def pages(draw: st.DrawFn) -> PageSpec:
    base = draw(st.integers(min_value=100, max_value=10**6))
    pool = [Decimal(base + 1013 * i).scaleb(-2) for i in range(12)]  # twelve distinct amounts
    requested_amount, meta_alt, trusted_alt = pool[0], pool[1], pool[2]
    nodes: list[NodeSpec] = []
    present = draw(st.sampled_from([True, True, True, False]))
    if present:
        nodes.append(
            NodeSpec(
                0,
                requested_amount,
                requested=True,
                identity=draw(st.sampled_from([URL, URL, URL, *REQUESTED_IDENTITIES])),
                context=draw(st.sampled_from([TOP, TOP, MAIN_ENTITY, ITEMLIST])),
                price_mode=draw(st.sampled_from(PRICE_MODES)),
                currency=draw(st.sampled_from(["EUR", None])),
                duplicated=draw(st.booleans()),
            )
        )
    for i in range(draw(st.sampled_from([0, 0, 1, 1, 2, 3, 4, 5]))):
        if present:
            identity = draw(st.sampled_from(FOREIGN_IDENTITIES))
            context = draw(st.sampled_from([TOP, ITEMLIST]))
        elif draw(st.booleans()):
            identity = draw(st.sampled_from(OBSERVABLY_FOREIGN))
            context = draw(st.sampled_from([TOP, ITEMLIST]))
        else:
            identity = draw(st.sampled_from(FOREIGN_IDENTITIES))
            context = ITEMLIST
        nodes.append(
            NodeSpec(
                i + 1,
                pool[3 + i],
                requested=False,
                identity=identity,
                context=context,
                price_mode=draw(st.sampled_from(PRICE_MODES)),
                currency=draw(st.sampled_from(["EUR", None])),
                duplicated=draw(st.booleans()),
            )
        )
    order = draw(st.permutations(range(len(nodes))))
    unscoped_pool = [pool[9], pool[10], pool[11], requested_amount]
    return PageSpec(
        nodes=tuple(nodes[i] for i in order),
        layout=draw(st.sampled_from(["scripts", "graph"])),
        page_meta=draw(st.sampled_from([None, None, requested_amount, meta_alt])),
        trusted=draw(st.sampled_from([None, None, requested_amount, trusted_alt])),
        unscoped=tuple(draw(st.lists(st.sampled_from(unscoped_pool), max_size=3, unique=True))),
        key_order_seed=draw(st.integers(min_value=0, max_value=50)),
    )


def _anchor(page: PageSpec) -> AnchorResult:
    rendered = render(page)
    return anchor_structured_page(rendered.payloads, REQUESTED, extra=rendered.extra)


def _outcome(result: AnchorResult) -> tuple[AnchorState, Money | None, str]:
    return result.state, result.price, result.reason


# ------------------------------------------------------------------ (a)


@settings(max_examples=2000, deadline=None)
@given(pages())
def test_no_foreign_price_is_ever_returned(page: PageSpec) -> None:
    result = _anchor(page)
    event(f"{result.state.value}:{result.reason}")
    if result.price is not None:
        assert result.price.amount in page.allowed_amounts, (page, result)


@settings(max_examples=500, deadline=None)
@given(pages(), st.sampled_from([URL, URL_RELATIVE, URL_TRACKING]), st.sampled_from(["EUR", None]))
def test_identified_readable_requested_product_is_found(
    page: PageSpec, identity: str, currency: str | None
) -> None:
    """Liveness: with agreeing attributed sources, the requested price is always read."""
    requested_amount = Decimal("4242.42")
    mine = NodeSpec(0, requested_amount, True, identity=identity, currency=currency)
    others = tuple(n for n in page.nodes if not n.requested)
    agreeing = PageSpec(
        nodes=(mine, *others),
        layout=page.layout,
        page_meta=None if page.page_meta is None else requested_amount,
        trusted=None if page.trusted is None else requested_amount,
        unscoped=page.unscoped,
        key_order_seed=page.key_order_seed,
    )
    result = _anchor(agreeing)
    assert result.state is AnchorState.FOUND, result
    assert result.price is not None
    assert result.price.amount == requested_amount
    assert result.reason == "owned"


@settings(max_examples=500, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.booleans(), st.sampled_from(REQUESTED_IDENTITIES), st.sampled_from(PRICE_MODES)
        ),
        min_size=1,
        max_size=4,
    ),
    st.lists(st.sampled_from([Decimal("12.34"), Decimal("56.78")]), max_size=2, unique=True),
)
def test_carousel_members_never_decide(
    members: list[tuple[bool, str, str]], unscoped: list[Decimal]
) -> None:
    """A carousel member is foreign whatever it carries, even the requested URL."""
    nodes = tuple(
        NodeSpec(
            i,
            Decimal(f"{20 + i}.00"),
            requested=is_requested and i == 0,
            identity=identity,
            context=ITEMLIST,
            price_mode=mode,
        )
        for i, (is_requested, identity, mode) in enumerate(members)
    )
    result = _anchor(PageSpec(nodes=nodes, unscoped=tuple(unscoped)))
    assert _outcome(result) == (AnchorState.AMBIGUOUS, None, "foreign_only")


# ------------------------------------------------------------------ (b)


@settings(max_examples=1000, deadline=None)
@given(pages(), st.data())
def test_observation_permutation_and_duplication_invariance(
    page: PageSpec, data: st.DataObject
) -> None:
    rendered = render(page)
    extracted = jsonld_observations(rendered.payloads, REQUESTED)
    if isinstance(extracted, AnchorResult):  # pragma: no cover - the generator is well formed
        raise AssertionError(extracted)
    observations = extracted + rendered.extra
    baseline = resolve_anchor(observations)
    permuted = tuple(data.draw(st.permutations(observations)))
    duplicates: tuple[Observation, ...] = ()
    if observations:
        duplicates = tuple(data.draw(st.lists(st.sampled_from(observations), max_size=6)))
    mixed = list(permuted + duplicates)
    data.draw(st.randoms(use_true_random=False)).shuffle(mixed)
    assert resolve_anchor(tuple(mixed)) == baseline


@settings(max_examples=500, deadline=None)
@given(pages(), st.data())
def test_document_order_layout_and_key_order_invariance(
    page: PageSpec, data: st.DataObject
) -> None:
    baseline = _outcome(_anchor(page))
    reordered = PageSpec(
        nodes=tuple(data.draw(st.permutations(page.nodes))),
        layout="graph" if page.layout == "scripts" else "scripts",
        page_meta=page.page_meta,
        trusted=page.trusted,
        unscoped=tuple(data.draw(st.permutations(page.unscoped))),
        key_order_seed=data.draw(st.integers(min_value=0, max_value=50)),
    )
    assert _outcome(_anchor(reordered)) == baseline


refs = st.sampled_from([f"r{i}" for i in range(10)])
keys = st.none() | st.sampled_from(["k0", "k1"])
moneys = st.none() | st.builds(
    Money,
    amount=st.sampled_from([Decimal("10"), Decimal("10.00"), Decimal("12.5")]),
    currency=st.sampled_from([None, "EUR", "USD"]),
)


@st.composite
def observations(draw: st.DrawFn) -> Observation:
    price = draw(moneys)
    return Observation(
        draw(st.sampled_from(["jsonld", "og", "css"])),
        draw(st.sampled_from(list(Ownership))),
        draw(refs),
        draw(keys),
        price,
        "" if price is not None else draw(st.sampled_from(["malformed", "financing_only"])),
    )


@settings(max_examples=1500, deadline=None)
@given(st.lists(observations(), max_size=8), st.data())
def test_arbitrary_observation_sets_are_order_and_duplication_invariant(
    items: list[Observation], data: st.DataObject
) -> None:
    """Even colliding, contradictory or currency-mixed sets decide the same way."""
    baseline = resolve_anchor(tuple(items))
    event(f"{baseline.state.value}:{baseline.reason}")
    shuffled = list(items) + (
        data.draw(st.lists(st.sampled_from(items), max_size=4)) if items else []
    )
    data.draw(st.randoms(use_true_random=False)).shuffle(shuffled)
    assert resolve_anchor(tuple(shuffled)) == baseline
    if baseline.state is AnchorState.FOUND:
        assert baseline.reason in {"owned", "attributed", "singleton_unknown"}
        assert all(o.ownership is not Ownership.UNSCOPED for o in baseline.deciding)


# ------------------------------------------------------------------ §5.2.6, exhaustively

R = Decimal("99.99")
META_BAD = Decimal("79.99")
UNSCOPED_AMOUNT = Decimal("12.34")


def _case_page(
    n: int,
    k: int,
    mode: str,
    price: str,
    meta: str,
    unscoped: bool,
    carousel: bool,
    req_currency: str | None,
    equal_prices: bool,
) -> PageSpec:
    nodes: list[NodeSpec] = []
    for j in range(n):
        if j == k:
            if mode == "requested_removed":
                continue
            nodes.append(
                NodeSpec(
                    j,
                    R,
                    True,
                    identity=URL if mode == "present" else NONE,
                    price_mode=price,
                    currency=req_currency,
                )
            )
        else:
            nodes.append(
                NodeSpec(
                    j,
                    R if equal_prices else Decimal(f"{10 + j}.49"),
                    False,
                    identity=NONE if mode == "absent" else URL,
                    context=ITEMLIST if carousel else TOP,
                )
            )
    return PageSpec(
        nodes=tuple(nodes),
        page_meta={"none": None, "agree": R, "disagree": META_BAD}[meta],
        unscoped=(UNSCOPED_AMOUNT,) if unscoped else (),
    )


def _absent_veto_disagrees(
    n: int, price: str, carousel: bool, equal_prices: bool, attributed_amount: Decimal
) -> bool:
    """Whether some readable unidentified node vetoes ``attributed_amount`` (mode "absent").

    Written from rule 2's veto, never by calling the resolver. In this mode the
    would-be requested node (``k``) always carries amount ``R`` and, unless its price
    is unreadable, is itself one of the unidentified nodes. The other ``n - 1`` nodes
    are unidentified too — unless ``carousel``, which turns them ``FOREIGN`` and out
    of the veto's reach — and are always readable, at ``R`` when ``equal_prices`` or a
    per-node amount that never equals ``R`` or ``META_BAD`` otherwise.
    """
    if price == READABLE and attributed_amount != R:
        return True  # node k itself disagrees
    if not carousel and n >= 2:
        if equal_prices:
            return attributed_amount != R
        return True  # a differing per-node amount always disagrees
    return False


def _expected(
    n: int,
    mode: str,
    price: str,
    meta: str,
    unscoped: bool,
    carousel: bool,
    cur: str | None,
    equal: bool,
) -> tuple[AnchorState, Money | None, str]:
    """The §5.2.6 table, written from the specification's case list."""
    found, none_ = AnchorState.FOUND, None
    if mode == "requested_removed":
        if meta == "agree":
            return found, Money(R, "EUR"), "attributed"
        if meta == "disagree":
            return found, Money(META_BAD, "EUR"), "attributed"
        if n - 1 >= 1:
            return AnchorState.AMBIGUOUS, none_, "foreign_only"
        if unscoped:
            return AnchorState.NO_CANDIDATE, none_, "unscoped_only"
        return AnchorState.NO_CANDIDATE, none_, "no_observation"
    if mode == "present":
        if price != READABLE:
            return AnchorState.NO_CANDIDATE, none_, "requested_unreadable"
        if meta == "disagree":
            return AnchorState.AMBIGUOUS, none_, "owned_sources_disagree"
        currency = "EUR" if (cur == "EUR" or meta == "agree") else None
        return found, Money(R, currency), "owned"
    # identities absent from every node
    if meta in ("agree", "disagree"):
        attributed_amount = R if meta == "agree" else META_BAD
        if _absent_veto_disagrees(n, price, carousel, equal, attributed_amount):
            return AnchorState.AMBIGUOUS, none_, "attributed_unknown_disagree"
        return found, Money(attributed_amount, "EUR"), "attributed"
    if n >= 2 and carousel:
        return AnchorState.AMBIGUOUS, none_, "foreign_only"
    if n >= 2:
        return AnchorState.AMBIGUOUS, none_, "multiple_unknown_nodes"
    if price == READABLE:
        return found, Money(R, cur), "singleton_unknown"
    return AnchorState.NO_CANDIDATE, none_, "unknown_unreadable"


def test_outcome_table_full_product() -> None:
    failures: list[str] = []
    processed = 0
    grid = itertools.product(
        range(1, 6),
        ["present", "absent", "requested_removed"],
        [READABLE, FINANCING, MALFORMED],
        ["none", "agree", "disagree"],
        [False, True],
        [False, True],
        ["EUR", None],
        [False, True],
    )
    for n, mode, price, meta, unscoped, carousel, cur, equal in grid:
        for k in range(n):
            page = _case_page(n, k, mode, price, meta, unscoped, carousel, cur, equal)
            got = _outcome(_anchor(page))
            want = _expected(n, mode, price, meta, unscoped, carousel, cur, equal)
            processed += 1
            if got != want:
                failures.append(f"{(n, k, mode, price, meta, unscoped, carousel, cur, equal)}")
                failures.append(f"   got {got}\n  want {want}")
    assert processed == 15 * 3 * 3 * 3 * 2 * 2 * 2 * 2
    assert not failures, "\n".join(failures[:20])


_f1_a = "https://shop.example.com/p/A"
_f1_b = "https://shop.example.com/p/B"
_f1_requested = RequestedIdentity.from_url(_f1_a)


def _f1_product(**fields: object) -> dict[str, object]:
    return {"@type": "Product", "offers": {"price": "9.99", "priceCurrency": "EUR"}, **fields}


def _f1_anchor(doc: object):
    return anchor_structured_page([json.dumps(doc)], _f1_requested)


@settings(max_examples=80, database=None)
@given(
    st.sampled_from([_f1_b, "/p/B", "https://other.example.net/p/B"]),
    st.text(alphabet="abcdef0123", min_size=1, max_size=8),
    st.booleans(),
)
def test_foreign_fragment_keeps_document_identity(base, fragment, contradict):
    fields = {"@id": base + "#" + fragment}
    if contradict:
        fields["url"] = _f1_a
    result = _f1_anchor(_f1_product(**fields))
    assert result.state is AnchorState.AMBIGUOUS
    assert result.reason == ("contradictory_identity" if contradict else "foreign_only")
    assert result.price is None
    control = _f1_anchor(_f1_product(**{"@id": _f1_a + "#" + fragment}))
    assert control.state is AnchorState.FOUND
    assert control.reason == "singleton_unknown"


@settings(max_examples=40, database=None)
@given(st.sampled_from([_f1_b, "/p/B"]), st.sampled_from(["https://shop.example.com/p/C", "/p/C"]))
def test_two_foreign_identities_cannot_be_rescued(first, second):
    bad = _f1_product(url=first, **{"@id": second})
    for documents in itertools.permutations([bad, _f1_product(url=_f1_a)]):
        result = anchor_structured_page([json.dumps(doc) for doc in documents], _f1_requested)
        assert result.reason == "contradictory_identity"
        assert result.price is None


@settings(max_examples=80, database=None)
@given(
    st.integers(min_value=1, max_value=10000),
    st.sampled_from([Ownership.UNKNOWN, Ownership.PAGE, Ownership.REQUESTED]),
)
def test_decimal_representation_does_not_change_whole_result(integer, owner):
    def obs(text: str, currency: str) -> Observation:
        return Observation("jsonld", owner, "same", "same", Money(Decimal(text), currency))

    a = obs(f"{integer}.0", "EUR")
    b = obs(str(integer), "EUR")
    c = obs(str(integer), "USD")
    values = (a, b, c)
    reference = resolve_anchor(values)
    for ordering in itertools.permutations(values):
        assert resolve_anchor(ordering) == reference
        assert resolve_anchor(ordering + ordering[:2]) == reference
    page = Observation("og", Ownership.PAGE, "meta", None, Money(Decimal(integer), "EUR"))
    unknown = tuple(
        Observation(o.source, Ownership.UNKNOWN, o.node_ref, o.node_key, o.price) for o in values
    )
    reference = resolve_anchor((page, *unknown))
    for ordering in itertools.permutations(unknown):
        assert resolve_anchor((page, *ordering)) == reference


# None of these edges transfers ownership, even when its immediate parent is a WebPage.
_NON_OWNING_EDGES = [
    "mentions",
    "about",
    "relatedLink",
    "isRelatedTo",
    "isSimilarTo",
    "hasPart",
    "item",
    "itemListElement",
    "hasVariant",
    "isVariantOf",
    "offers",
    "itemOffered",
]
# An edge name that collides with a field structured_data.py treats as node identity
# ("url", plus the scalar identifier fields) turns the wrapping node from an arbitrary
# foreign carrier into a genuinely malformed Product/Offer node once that node's own
# @type is "Product": the decoder raises StructureError on it (terminal AMBIGUOUS
# malformed_structure, covered by test_not_well_formed.py's identity-field tests),
# which is a different property than "nested foreign price never returned" and must
# not be generated here.
_IDENTITY_FIELD_NAMES = frozenset({"url", *_IDENTIFIER_FIELDS})
_RANDOM_NAME = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=16).filter(
    lambda name: name not in _IDENTITY_FIELD_NAMES
)


@st.composite
def nested_foreign_pages(draw):
    from ._docgen import NestedForeignSpec

    depth = draw(st.integers(min_value=1, max_value=6))
    # The last arc under the root (the edge connecting the top-level node to its
    # immediate child) is excluded from "mainEntity": at that position the edge is a
    # genuine top-level MAIN grant, not a hostile nested one, and could legally surface
    # the foreign leaf's price when depth == 1. Every other (intermediate) arc may be
    # "mainEntity", so the property exercises the fix: a non-top WebPage's own
    # mainEntity edge must never grant PAGE ownership to its child.
    inner_edge = st.one_of(st.sampled_from(_NON_OWNING_EDGES), st.just("mainEntity"), _RANDOM_NAME)
    outer_edge = st.one_of(st.sampled_from(_NON_OWNING_EDGES), _RANDOM_NAME)
    inner_edges = draw(st.lists(inner_edge, min_size=depth - 1, max_size=depth - 1))
    edges = tuple(inner_edges) + (draw(outer_edge),)
    types = draw(
        st.lists(
            st.one_of(
                st.sampled_from(
                    [
                        "WebPage",
                        "CreativeWork",
                        "Product",
                        "ListItem",
                        "CollectionPage",
                        "Offer",
                    ]
                ),
                _RANDOM_NAME,
            ),
            min_size=depth,
            max_size=depth,
        )
    )
    return NestedForeignSpec(
        tuple(edges),
        tuple(types),
        draw(st.sampled_from([None, REQUESTED_URL, "https://shop.example.com/p/foreign"])),
        draw(st.sampled_from(["absent", "resolved", "unresolved"])),
        draw(st.booleans()),
        draw(st.integers(min_value=1, max_value=100000)),
        draw(st.sampled_from(["scripts", "graph"])),
    )


@settings(max_examples=1200, deadline=None)
@given(nested_foreign_pages())
def test_arbitrary_nested_foreign_price_never_returned(page):
    import json

    result = anchor_structured_page([json.dumps(doc) for doc in page.documents()], REQUESTED)
    if result.price is not None:
        assert result.price == Money(Decimal(page.requested_amount), "EUR")
        assert result.price.amount != Decimal(page.requested_amount + 100)
    if page.reference == "unresolved":
        assert result.reason == "unresolved_main_entity"
        assert result.price is None
    # Same hostile subtree, with a readable identified candidate and a resolvable subject.
    live = anchor_structured_page([json.dumps(doc) for doc in page.documents(live=True)], REQUESTED)
    assert (live.state, live.reason, live.price) == (
        AnchorState.FOUND,
        "owned",
        Money(Decimal(page.requested_amount), "EUR"),
    )
