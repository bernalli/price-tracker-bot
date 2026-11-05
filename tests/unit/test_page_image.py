"""Image candidates retain the document and identity of the price decision."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from bs4 import BeautifulSoup, Tag
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.page_image import ImageDocument, PriceImageBinding, extract_page_image
from price_tracker.core.scraper_base import ProductInfo

URL = "https://shop.example.com/products/a"
IMAGE = "https://cdn.example.com/a.jpg"
OTHER = "https://cdn.example.com/b.jpg"


def product(image: Any = IMAGE, **extra: Any) -> dict[str, Any]:
    return {"@type": "Product", "url": URL, "image": image, **extra}


def document(payloads: list[Any], meta: str = "") -> ImageDocument:
    soup = BeautifulSoup(
        meta
        + "".join(f'<script type="application/ld+json">{json.dumps(p)}</script>' for p in payloads),
        "lxml",
    )
    return ImageDocument.parse(soup)


def binding(doc: ImageDocument, **extra: Any) -> PriceImageBinding:
    refs = tuple(ref for ref, node in doc.nodes.items() if node.get("url") == URL)
    return PriceImageBinding(
        document=doc,
        requested_url=URL,
        product_refs=refs,
        product_identity=URL,
        identity_verified=True,
        **extra,
    )


def extract(doc: ImageDocument, **extra: Any) -> str | None:
    return extract_page_image(doc.soup, page_url=URL, price_binding=binding(doc, **extra))


def test_product_info_has_no_image_until_one_is_attributed() -> None:
    assert ProductInfo().image_url is None


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        (IMAGE, IMAGE),
        (["javascript:bad", IMAGE, OTHER], IMAGE),
        ({"@type": "ImageObject", "contentUrl": IMAGE}, IMAGE),
        ([{"url": IMAGE}], IMAGE),
        ("/images/a.jpg", "https://shop.example.com/images/a.jpg"),
        (None, None),
        (123, None),
        ({"width": 300}, None),
        ([True, {}, 123], None),
    ],
)
def test_only_images_of_the_price_owner_are_candidates(image: Any, expected: str | None) -> None:
    doc = document([{"@graph": [product(image)]}])
    assert extract(doc) == expected


@pytest.mark.parametrize("kind", [123, None, {}, ["Product", 123], ["Product", ""], True])
def test_malformed_types_cannot_authorize_an_image(kind: Any) -> None:
    doc = document([product(**{"@type": kind})])
    assert extract(doc) is None


def test_missing_unverified_foreign_and_wrong_document_bindings_are_refused() -> None:
    doc = document([product()])
    assert extract_page_image(doc.soup, page_url=URL) is None
    good = binding(doc)
    for bad in (
        replace(good, identity_verified=False),
        replace(good, product_identity="https://shop.example.com/products/b"),
        replace(good, requested_url="https://shop.example.com/products/b"),
        replace(good, product_refs=("jsonld:99",)),
        replace(good, document=document([product()])),
    ):
        assert extract_page_image(doc.soup, page_url=URL, price_binding=bad) is None


@settings(max_examples=50, deadline=None)
@given(st.permutations([0, 1, 2]))
def test_script_graph_order_and_foreign_duplicates_do_not_change_the_owner(
    order: list[int],
) -> None:
    nodes = [
        product(),
        product(OTHER, url="https://shop.example.com/products/b"),
        {"@type": "ProductGroup", "image": OTHER},
    ]
    for payloads in (
        [nodes[i] for i in order],
        [{"@graph": [nodes[i] for i in order]}],
        [[nodes[i] for i in order]],
    ):
        doc = document(payloads)
        assert extract(doc) == IMAGE
        doc = document([*payloads, nodes[1], nodes[1]])
        assert extract(doc) == IMAGE


@pytest.mark.parametrize("reverse", [False, True])
def test_equivalent_owner_duplicates_are_safe_and_conflicts_are_terminal(reverse: bool) -> None:
    for images, expected in (([IMAGE, IMAGE], IMAGE), ([IMAGE, OTHER], None)):
        if reverse:
            images.reverse()
        doc = document(
            [product(image) for image in images],
            '<meta property="og:image" content="https://cdn.example.com/fallback.jpg">',
        )
        assert extract(doc, page_scope="product") == expected


@pytest.mark.parametrize("scope", [None, "product"])
def test_page_metadata_requires_price_attribution_and_prefers_secure(scope: str | None) -> None:
    doc = document(
        [product(None)],
        '<meta property="og:image" content="http://cdn.example.com/a.jpg">'
        '<meta property="og:image:secure_url" content="https://cdn.example.com/a.jpg">',
    )
    assert extract(doc, page_scope=scope) == (IMAGE if scope else None)


def test_relative_page_metadata_is_normalized_only_with_a_binding() -> None:
    doc = document([product(None)], '<meta property="og:image" content="/images/a.jpg">')
    assert extract(doc, page_scope="product") == "https://shop.example.com/images/a.jpg"


@pytest.mark.parametrize("field", ["canonical", "og:url"])
def test_contradictory_page_echoes_prevent_image_attribution(field: str) -> None:
    meta = (
        '<link rel="canonical" href="https://shop.example.com/products/b">'
        if field == "canonical"
        else '<meta property="og:url" content="https://shop.example.com/products/b">'
    )
    doc = document([product()], meta)
    assert extract(doc, page_scope="product") is None


def test_binding_cannot_be_reused_after_the_document_changes() -> None:
    doc = document([product()])
    good = binding(doc)
    script = doc.soup.find("script")
    assert isinstance(script, Tag)
    script.string = json.dumps(product(OTHER))
    assert extract_page_image(doc.soup, page_url=URL, price_binding=good) is None


@pytest.mark.parametrize(
    "malformed", ["{", '{"image":"a","image":"b"}', '{"@type":', "[] trailing"]
)
def test_invalid_scripts_are_skipped_without_lending_an_image(malformed: str) -> None:
    doc = document([product()])
    new = doc.soup.new_tag("script", type="application/ld+json")
    new.string = malformed
    doc.soup.append(new)
    doc = ImageDocument.parse(doc.soup)
    assert extract(doc) == IMAGE


@pytest.mark.parametrize("count", [10, 11])
def test_script_limit_is_checked_before_selecting_a_prefix(count: int) -> None:
    doc = document([product()] + [{}] * (count - 1))
    assert extract(doc) == (IMAGE if count == 10 else None)


@pytest.mark.parametrize("count", [20, 21, 30])
def test_candidate_budget_counts_even_equivalent_duplicates(count: int) -> None:
    doc = document([product([IMAGE] * count)])
    assert extract(doc) == (IMAGE if count == 20 else None)


@pytest.mark.parametrize("over", [False, True])
def test_node_budget_rejects_the_whole_document_before_prefix_selection(over: bool) -> None:
    # Product has four scalar children: type, url, image and an arbitrary array.
    # Root plus those four nodes and array elements reaches exactly 10,000.
    doc = document([product(extra=[0] * (9995 + int(over)))])
    assert extract(doc) == (None if over else IMAGE)


def test_selected_variant_never_borrows_a_sibling_image() -> None:
    node = {
        "@type": "ProductGroup",
        "url": URL,
        "image": IMAGE,
        "hasVariant": [
            product(None, url=URL + "?variant=one"),
            product(OTHER, url=URL + "?variant=two"),
        ],
    }
    doc = document([node])
    good = binding(doc, variant_identity="one", variant_refs=("jsonld:0/hasVariant/0",))
    assert extract_page_image(doc.soup, page_url=URL, price_binding=good) is None
    compatible = replace(good, product_image_compatible=True)
    assert extract_page_image(doc.soup, page_url=URL, price_binding=compatible) == IMAGE
    doc = document([{**node, "hasVariant": [product(OTHER, url=URL + "?variant=one")]}])
    good = binding(doc, variant_identity="one", variant_refs=("jsonld:0/hasVariant/0",))
    assert extract_page_image(doc.soup, page_url=URL, price_binding=good) == OTHER


def test_variant_page_scope_needs_positive_selection_evidence() -> None:
    doc = document([product(None)], f'<meta property="og:image" content="{IMAGE}">')
    good = binding(doc, variant_identity="one", page_scope="product")
    assert extract_page_image(doc.soup, page_url=URL, price_binding=good) is None
    good = replace(good, page_scope="variant")
    assert extract_page_image(doc.soup, page_url=URL, price_binding=good) == IMAGE


def test_an_action_object_is_visited_only_after_validating_its_type() -> None:
    for kind, expected in (("BuyAction", IMAGE), (["BuyAction", 1], None), (1, None)):
        doc = document([{"@type": kind, "object": product()}])
        assert extract(doc) == expected


JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=30),
    lambda child: (
        st.lists(child, max_size=6) | st.dictionaries(st.text(max_size=15), child, max_size=6)
    ),
    max_leaves=40,
)


@settings(max_examples=150, deadline=None)
@given(JSON)
def test_arbitrary_json_structures_never_escape_as_exceptions(value: Any) -> None:
    doc = document([product(value), value])
    result = extract(doc)
    assert result is None or result.startswith("https://")


def test_truncation_at_every_boundary_never_yields_an_image() -> None:
    raw = json.dumps(product())
    for offset in range(len(raw)):
        soup = BeautifulSoup(f'<script type="application/ld+json">{raw[:offset]}</script>', "lxml")
        doc = ImageDocument.parse(soup)
        assert extract(doc) is None


@settings(max_examples=30, deadline=None)
@given(st.permutations([0, 1, 2]))
def test_foreign_recommendations_and_same_price_nodes_never_lend_images(order: list[int]) -> None:
    nodes = [
        product(None, offers={"price": "10"}),
        product(OTHER, url="https://shop.example.com/products/b", offers={"price": "10"}),
        {"@type": "ItemList", "itemListElement": [product(OTHER)]},
    ]
    doc = document([{"@graph": [nodes[i] for i in order]}])
    assert extract(doc) is None


@pytest.mark.parametrize("kind", ["scripts", "candidates", "nodes"])
@pytest.mark.parametrize("reverse", [False, True])
def test_conflicts_beyond_each_budget_never_select_a_prefix(kind: str, reverse: bool) -> None:
    if kind == "scripts":
        payloads: list[Any] = [product()] + [{}] * 9 + [product(OTHER)]
    elif kind == "candidates":
        payloads = [[product()] * 20 + [product(OTHER)]]
    else:
        payloads = [[product(), *([{}] * 9999), product(OTHER)]]
    if reverse:
        payloads.reverse()
        if isinstance(payloads[0], list):
            payloads[0].reverse()
    doc = document(payloads)
    assert extract(doc) is None


def test_equal_price_variants_do_not_imply_a_representative_image() -> None:
    node = {
        "@type": "ProductGroup",
        "url": URL,
        "hasVariant": [
            product(IMAGE, url=URL + "?variant=one", price="10"),
            product(OTHER, url=URL + "?variant=two", price="10"),
        ],
    }
    doc = document([node])
    assert extract(doc) is None
    doc = document([{**node, "image": IMAGE}])
    assert extract(doc) == IMAGE


def test_script_detection_reads_at_most_eleven_payloads() -> None:
    doc = document([product()] * 100)
    assert len(doc.payloads) == 11
    assert extract(doc) is None


def test_the_original_variant_selector_is_preserved_in_the_binding() -> None:
    selected_url = URL + "?variant=one"
    doc = document([product(hasVariant=[product(OTHER, url=selected_url)])])
    selected = binding(doc, variant_identity="one", variant_refs=("jsonld:0/hasVariant/0",))
    selected = replace(selected, requested_url=selected_url)
    assert extract_page_image(doc.soup, page_url=selected_url, price_binding=selected) == OTHER
    selected = replace(selected, requested_url=URL + "?variant=two")
    assert extract_page_image(doc.soup, page_url=selected_url, price_binding=selected) is None


def test_a_variant_source_cannot_identify_a_different_product() -> None:
    doc = document(
        [
            product(
                hasVariant=[product(OTHER, url="https://shop.example.com/products/b?variant=one")]
            )
        ]
    )
    selected = binding(doc, variant_identity="one", variant_refs=("jsonld:0/hasVariant/0",))
    assert extract_page_image(doc.soup, page_url=URL, price_binding=selected) is None
