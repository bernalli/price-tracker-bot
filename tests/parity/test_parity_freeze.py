"""Freeze verification: the recorded parity of the old parsers against the corpus.

Every literal below (counts, sha256 digests) was compiled by running
``scripts/record_parity.py`` on this tree and reading its output, per the execution
checklist. A mismatch here means the pinned files drifted from what the script (or
the live functions/grammar it freezes) currently produce.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from scripts import record_parity as rp

from price_tracker.core import scraper_base as sb

ROOT = Path(__file__).resolve().parents[2]
PARITY_DIR = ROOT / "tests" / "parity"
CORPUS_PATH = PARITY_DIR / "corpus.json"
MANIFEST_PATH = PARITY_DIR / "manifest.json"
FROZEN_PATHS = {fn: PARITY_DIR / f"{fn}.frozen.json" for fn in rp.KNOWN_FUNCTIONS}
EXCEPTIONS_PATHS = {fn: PARITY_DIR / f"{fn}.exceptions.json" for fn in rp.KNOWN_FUNCTIONS}

CORPUS_COUNT = 852
SECTION_COUNTS = (108, 147, 597)
CORPUS_SHA256 = "5a3102bb0e23ec42"
GENERATED_SHA256 = "f7ac02ffe3ce6056"
FROZEN_SHA256 = {
    "parse_price": "db6292be7cb50da6b5bdeb09e64bca659f66003d1e9e39a85464b37b2064772e",
    "detect_currency": "a3692cee466f4c86e9dc12bd8a4bc6ef58b66418a863b880e279c9d674914732",
}
EXCEPTIONS_COUNT = {"parse_price": 290, "detect_currency": 225}
EXCEPTIONS_SHA256 = {
    "parse_price": "c014f9b13c7defa3804b4c57f45f3d5e4e9405b0864ead662b60db70b3945d33",
    "detect_currency": "0e40ee13a8823858781e4d819923179aff68ec5dbcd72782a85b21867d5be712",
}


def _sha16(items: object) -> str:
    return hashlib.sha256(json.dumps(items, ensure_ascii=False).encode()).hexdigest()[:16]


def _full_corpus(data: dict[str, Any]) -> list[str | None]:
    harvested = data["harvested"]
    curated = data["curated"]
    generated = data["generated"]["inputs"]
    return sorted(set(harvested) | set(curated) | set(generated), key=rp.sort_key)


def test_corpus_is_well_formed_and_pinned() -> None:
    data = rp.load_corpus(CORPUS_PATH)
    counts = (len(data["harvested"]), len(data["curated"]), len(data["generated"]["inputs"]))
    assert counts == SECTION_COUNTS
    assert data["count"] == CORPUS_COUNT
    assert data["count"] >= 800
    assert _sha16(_full_corpus(data)) == CORPUS_SHA256


def test_generated_section_is_reproducible() -> None:
    data = rp.load_corpus(CORPUS_PATH)
    requested = data["generated"]["requested"]
    both = set(data["harvested"]) | set(data["curated"])
    dedup = rp._dedup_preserve_order(rp.generate(requested))  # noqa: SLF001 - same module
    expected = sorted((x for x in dedup if x not in both), key=rp.sort_key)
    assert expected == data["generated"]["inputs"]
    assert _sha16(expected) == GENERATED_SHA256


def test_curated_section_is_the_closed_union() -> None:
    data = rp.load_corpus(CORPUS_PATH)
    union = set(rp.TEST_LITERALS) | set(rp.GRAMMAR_ROWS) | set(rp.HOSTILE) | set(rp.URL_SHAPED)
    harvested = set(data["harvested"])
    expected = sorted((x for x in union - harvested if x is not None), key=rp.sort_key)
    assert data["curated"] == expected
    assert len(data["curated"]) == 147


@pytest.mark.parametrize("fn", rp.KNOWN_FUNCTIONS)
def test_frozen_files_are_pinned_and_cover_the_corpus(fn: str) -> None:
    path = FROZEN_PATHS[fn]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == FROZEN_SHA256[fn]
    manifest = rp.load_manifest(MANIFEST_PATH)
    frozen = rp.load_frozen(path, manifest=manifest)
    corpus = rp.load_corpus(CORPUS_PATH)
    assert [row["input"] for row in frozen["rows"]] == _full_corpus(corpus)


@pytest.mark.parametrize("fn", rp.KNOWN_FUNCTIONS)
def test_frozen_callable_is_unchanged_or_declared_wired(fn: str) -> None:
    manifest = rp.load_manifest(MANIFEST_PATH)
    cfg = manifest["functions"][fn]
    live = rp.resolve(cfg["frozen"], "test_frozen")
    live_sha = hashlib.sha256(inspect.getsource(live).encode()).hexdigest()
    assert (live_sha == cfg["source_sha256"]) != cfg["wired"]

    frozen = rp.load_frozen(FROZEN_PATHS[fn], manifest=manifest)
    diffs: list[tuple[str | None, str | None, str | None]] = []
    for row in frozen["rows"]:
        s = row["input"]
        new = rp.serialize(live(s))
        if new != row["old"]:
            diffs.append((s, row["old"], new))

    if not cfg["wired"]:
        assert diffs == []
    else:
        exceptions = rp.load_exceptions(EXCEPTIONS_PATHS[fn], manifest=manifest, frozen=frozen)
        expected = {(row["input"], row["old"], row["new"]) for row in exceptions["rows"]}
        assert set(diffs) == expected


@pytest.mark.parametrize("fn", rp.KNOWN_FUNCTIONS)
def test_exceptions_equal_the_live_diff(fn: str) -> None:
    manifest = rp.load_manifest(MANIFEST_PATH)
    frozen = rp.load_frozen(FROZEN_PATHS[fn], manifest=manifest)
    exceptions = rp.load_exceptions(EXCEPTIONS_PATHS[fn], manifest=manifest, frozen=frozen)
    assert exceptions["count"] == EXCEPTIONS_COUNT[fn]

    subject = manifest["functions"][fn]["subject"]
    assert subject is not None
    computed = []
    for row in frozen["rows"]:
        s, old = row["input"], row["old"]
        new = None if s is None else rp.serialize(rp._call_subject(subject, s))  # noqa: SLF001
        if new == old:
            continue
        kind, cls = rp.classify(s, old, new, function=fn)
        computed.append({"input": s, "old": old, "new": new, "kind": kind, "class": cls})

    actual = [
        {key: row[key] for key in ("input", "old", "new", "kind", "class")}
        for row in exceptions["rows"]
    ]
    assert computed == actual

    for row in exceptions["rows"]:
        if row["class"].endswith("_other"):
            assert row["note"] != ""

    assert hashlib.sha256(EXCEPTIONS_PATHS[fn].read_bytes()).hexdigest() == EXCEPTIONS_SHA256[fn]


@pytest.mark.parametrize("fn", rp.KNOWN_FUNCTIONS)
def test_no_exceptions_file_without_a_subject(fn: str) -> None:
    manifest = rp.load_manifest(MANIFEST_PATH)
    subject = manifest["functions"][fn]["subject"]
    path = EXCEPTIONS_PATHS[fn]
    if subject is None:
        assert not path.exists()
    else:
        assert path.exists()


URL_SHAPED_RE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)*(?:example(?:\.(?:com|net|org))?|[a-z0-9-]+\.(?:invalid|test))"
    r"(?:/\S*)?$"
)
FORBIDDEN_RE = re.compile(r"://|www\.|\.(?:com|de|it|net|org|co\.uk)\b")
WORD_RE = re.compile(r"[A-Za-z]{4,}")
ALLOWED_WORDS = {
    "from",
    "Was",
    "now",
    "incl",
    "krone",
    "EURO",
    "Infinity",
    "NaN",
    "not",
    "price",
    "each",
    "solo",
    "nur",
} | set(rp.KNOWN_TOKENS)


def test_corpus_has_no_urls_hostnames_or_names() -> None:
    for s in rp.URL_SHAPED:
        assert URL_SHAPED_RE.fullmatch(s), s

    data = rp.load_corpus(CORPUS_PATH)
    url_shaped_set = set(rp.URL_SHAPED)
    for entry in _full_corpus(data):
        if entry is None or entry in url_shaped_set:
            continue
        assert FORBIDDEN_RE.search(entry) is None, entry
        for word in WORD_RE.findall(entry):
            assert word in ALLOWED_WORDS, (entry, word)


@pytest.mark.parametrize("fn", rp.KNOWN_FUNCTIONS)
def test_frozen_values_are_canonical(fn: str) -> None:
    manifest = rp.load_manifest(MANIFEST_PATH)
    assert manifest["functions"][fn]["wired"] is True
    frozen = rp.load_frozen(FROZEN_PATHS[fn], manifest=manifest)
    if fn == "parse_price":
        for row in frozen["rows"]:
            if row["old"] is not None:
                assert rp.value_ok_for_function("parse_price", row["old"])
        return

    for row in frozen["rows"]:
        if row["old"] is not None:
            assert re.fullmatch(r"[A-Z]{3}", row["old"])


def test_classifier_is_total_on_examples(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = rp.load_manifest(MANIFEST_PATH)
    frozen_pp = rp.load_frozen(FROZEN_PATHS["parse_price"], manifest=manifest)
    old_pp = {row["input"]: row["old"] for row in frozen_pp["rows"]}
    frozen_dc = rp.load_frozen(FROZEN_PATHS["detect_currency"], manifest=manifest)
    old_dc = {row["input"]: row["old"] for row in frozen_dc["rows"]}

    cases: list[tuple[str, str | None, str | None, str, str]] = []

    def loss(s: str, cls: str) -> None:
        old = old_pp[s]
        assert old is not None, s
        cases.append((s, old, None, "loss", cls))

    loss("0", "zero")
    loss("1000000001", "over_bound")
    loss("1" * 65, "over_length")
    loss("NUL\x00 12", "control_or_format_char")
    loss("١٢٣٤", "non_latin_digits")  # "١٢٣٤"
    loss("-5", "sign_or_exponent")
    loss("Was 20 now 10", "residual_words")
    loss("EUR 10 USD", "two_tokens")
    loss("10 nok", "token_case")
    loss("JPY 4.78", "precision_exceeded")
    loss("12 34", "second_number")
    loss("1.299", "three_digit_ambiguity")
    loss("0.999", "three_digit_ambiguity")
    loss("1.234,567,890", "three_decimals_rejected")
    loss(",99", "dangling_separator")
    loss("1,23,45", "invalid_grouping")
    loss("12٫50", "loss_other")  # "12٫50"

    cases.append(("kr. 89", "0.89", "89", "value", "token_with_dot"))
    cases.append(("1234.567", "1234567", "1234.567", "value", "three_decimals"))
    cases.append(("Fr. 2364826", None, "2364826", "gain", "token_with_dot"))

    for s, old, new, kind, cls in cases:
        got = rp.classify(s, old, new, function="parse_price")
        assert got == (kind, cls), (s, got, (kind, cls))

    well_formed = [
        "12,50",
        "1.234,56",
        "EUR 12,50",
        "1,299.99",
        "1 234,56",
        "$1,234.56",
        "19,-",
        "1.234,-",
    ]
    for s in well_formed:
        assert rp.classify(s, "1", None, function="parse_price") == ("loss", "loss_other")

    with pytest.raises(ValueError, match="nope"):
        rp.classify("x", "USD", None, function="nope")

    currency_cases: list[tuple[str, str, str]] = [
        ("https://example.com/gbp/1", "loss", "url_shaped"),
        ("EUR 10 USD", "loss", "two_tokens"),
        ("EURO 10", "loss", "token_glued_to_word"),
        ("10 nok", "loss", "token_case"),
        ("10 €/kg", "loss", "residual_words"),
        ("$29.99", "loss", "shared_symbol"),
        ("1,164,921\xa0CHF – 24,99", "loss", "token_inside"),
        ("R$ 47,57", "value", "symbol_superseded"),
        ("1 Fr.", "gain", "symbol_added"),
        ("BHD 1.234", "gain", "iso_code_added"),
    ]
    for s, kind, cls in currency_cases:
        old = old_dc[s]
        new = None if kind == "loss" else sb.detect_currency(s)
        got = rp.classify(s, old, new, function="detect_currency")
        assert got == (kind, cls), (s, got, (kind, cls))

    # Negative property: a currency engine that reads nothing at the edges never invents
    # a class from a predicate that depends on it, except the one it demonstrably breaks.
    monkeypatch.setattr(rp, "_edge_tokens", lambda s: [])
    no_edge_cases = [
        ("129,00 zł", "PLN"),
        ("12,99 €", "EUR"),
        ("€29.99", "EUR"),
        ("12.50 USD", "USD"),
        ("CHF 25.00", "CHF"),
    ]
    for s, old in no_edge_cases:
        assert rp.classify(s, old, None, function="detect_currency") == ("loss", "loss_other")
    assert rp.classify("1,164,921 CHF – 24,99", "CHF", None, function="detect_currency") == (
        "loss",
        "token_inside",
    )
    monkeypatch.undo()

    subject_pp = manifest["functions"]["parse_price"]["subject"]
    assert subject_pp is not None
    for row in frozen_pp["rows"]:
        s, old = row["input"], row["old"]
        new = None if s is None else rp.serialize(rp._call_subject(subject_pp, s))  # noqa: SLF001
        if new == old:
            continue
        kind, cls = rp.classify(s, old, new, function="parse_price")
        assert cls in rp.ALL_CLASSES["parse_price"][kind]

    subject_dc = manifest["functions"]["detect_currency"]["subject"]
    assert subject_dc is not None
    for row in frozen_dc["rows"]:
        s, old = row["input"], row["old"]
        new = None if s is None else rp.serialize(rp._call_subject(subject_dc, s))  # noqa: SLF001
        if new == old:
            continue
        kind, cls = rp.classify(s, old, new, function="detect_currency")
        assert cls in rp.ALL_CLASSES["detect_currency"][kind]


def test_scraper_base_import_does_not_load_the_price_core() -> None:
    """``core/fetch.py`` imports ``scraper_base`` for block detection: it must stay clean."""
    before = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import price_tracker.core.scraper_base; print(sorted(sys.modules))",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    before_modules = before.stdout
    assert "price_tracker.core.pricegrammar" not in before_modules
    assert "price_tracker.core.currencies" not in before_modules
    assert "price_tracker.core.money" not in before_modules

    after = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import price_tracker.core.scraper_base as sb; "
            "sb.parse_price('1'); print(sorted(sys.modules))",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    assert "price_tracker.core.pricegrammar" in after.stdout


def test_not_well_formed_fixture_old_values_match_the_frozen_oracle() -> None:
    """The literal ``old`` values of the not-well-formed fixture are the ``83a91f6`` oracle."""
    from tests.parity.test_record_parity_not_well_formed import OLD_DETECT, OLD_PARSE

    manifest = rp.load_manifest(MANIFEST_PATH)
    frozen_pp = rp.load_frozen(FROZEN_PATHS["parse_price"], manifest=manifest)
    oracle_pp = {row["input"]: row["old"] for row in frozen_pp["rows"]}
    frozen_dc = rp.load_frozen(FROZEN_PATHS["detect_currency"], manifest=manifest)
    oracle_dc = {row["input"]: row["old"] for row in frozen_dc["rows"]}

    for s, old in OLD_PARSE.items():
        assert old == oracle_pp[s], s
    for s, old in OLD_DETECT.items():
        assert old == oracle_dc[s], s
