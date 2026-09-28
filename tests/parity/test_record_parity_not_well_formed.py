"""Property tests for the record_parity loaders: every deformation must be refused.

Each case builds a small, self-consistent, valid set of parity files in ``tmp_path``,
applies exactly one deformation, and asserts that the matching ``load_*`` function
raises :class:`record_parity.ParityFormatError`. A positive control loads the intact
copy to prove the fixture itself is valid.

The fixture never depends on the real ``tests/parity/*.json`` data (harvested from a
real pytest run, or recorded from the live parser): it is built here from the live
functions and the normative constants of ``record_parity`` itself, so this file does
not need those other files to exist yet.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from scripts import record_parity as rp

from price_tracker.core import scraper_base as sb

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "record_parity.py"

# The four fixed literals of the fixture, at the value the parser at ``83a91f6`` (the
# base commit this parity freeze pins) produced — the oracle, not a value read from the
# live (now wired) code, which no longer represents the old behaviour.
OLD_PARSE: dict[str | None, str | None] = {
    None: None,
    "$0.00": "0.00",
    "1.234,56": "1234.56",
    "kr. 89": "0.89",
}
OLD_DETECT: dict[str | None, str | None] = {
    None: None,
    "$0.00": "USD",
    "1.234,56": None,
    "kr. 89": "SEK",
}


def _build_valid() -> dict[str, Any]:
    """Build a small, fully self-consistent, valid corpus/manifest/frozen/exceptions set."""
    harvested: list[str | None] = [None, "$0.00"]
    curated = sorted(["1.234,56", "kr. 89"], key=rp.sort_key)
    requested = 5
    both = set(harvested) | set(curated)
    dedup = rp._dedup_preserve_order(rp.generate(requested))  # noqa: SLF001 - same module, test-only
    generated = sorted((x for x in dedup if x not in both), key=rp.sort_key)

    corpus: dict[str, Any] = {
        "schema": 1,
        "count": len(harvested) + len(curated) + len(generated),
        "harvested": harvested,
        "curated": curated,
        "generated": {"seed": "test-seed", "requested": requested, "inputs": generated},
    }

    manifest: dict[str, Any] = {
        "schema": 1,
        "base_commit": "0" * 40,
        "functions": {
            "parse_price": {
                "frozen": "price_tracker.core.scraper_base:parse_price",
                "source_sha256": "0" * 64,
                "subject": "price_tracker.core.scraper_base:parse_price",
                "wired": True,
            },
            "detect_currency": {
                "frozen": "price_tracker.core.scraper_base:detect_currency",
                "source_sha256": "0" * 64,
                "subject": "price_tracker.core.scraper_base:detect_currency",
                "wired": True,
            },
        },
    }

    full_inputs = sorted(set(harvested) | set(curated) | set(generated), key=rp.sort_key)

    frozen_pp: dict[str, Any] = {
        "schema": 1,
        "function": "parse_price",
        "count": len(full_inputs),
        "rows": [
            {
                "input": s,
                "old": OLD_PARSE[s] if s in OLD_PARSE else rp.serialize(sb.parse_price(s)),
            }
            for s in full_inputs
        ],
    }
    frozen_dc: dict[str, Any] = {
        "schema": 1,
        "function": "detect_currency",
        "count": len(full_inputs),
        "rows": [
            {
                "input": s,
                "old": OLD_DETECT[s] if s in OLD_DETECT else rp.serialize(sb.detect_currency(s)),
            }
            for s in full_inputs
        ],
    }

    exception_rows_pp: list[dict[str, Any]] = []
    for row in frozen_pp["rows"]:
        s, old = row["input"], row["old"]
        if s not in OLD_PARSE:
            continue
        new = None if s is None else rp.serialize(sb.parse_price(s))
        if new == old:
            continue
        kind, cls = rp.classify(s, old, new, function="parse_price")
        exception_rows_pp.append(
            {"input": s, "old": old, "new": new, "kind": kind, "class": cls, "note": ""}
        )
    assert exception_rows_pp, "the fixture must exercise at least one parse_price divergence"
    exceptions_pp: dict[str, Any] = {
        "schema": 1,
        "function": "parse_price",
        "subject": "price_tracker.core.scraper_base:parse_price",
        "count": len(exception_rows_pp),
        "rows": exception_rows_pp,
    }

    exception_rows_dc: list[dict[str, Any]] = []
    for row in frozen_dc["rows"]:
        s, old = row["input"], row["old"]
        if s not in OLD_DETECT:
            continue
        new = None if s is None else rp.serialize(sb.detect_currency(s))
        if new == old:
            continue
        kind, cls = rp.classify(s, old, new, function="detect_currency")
        exception_rows_dc.append(
            {"input": s, "old": old, "new": new, "kind": kind, "class": cls, "note": ""}
        )
    assert exception_rows_dc, "the fixture must exercise at least one detect_currency divergence"
    exceptions_dc: dict[str, Any] = {
        "schema": 1,
        "function": "detect_currency",
        "subject": "price_tracker.core.scraper_base:detect_currency",
        "count": len(exception_rows_dc),
        "rows": exception_rows_dc,
    }

    return {
        "corpus": corpus,
        "manifest": manifest,
        "frozen_pp": frozen_pp,
        "frozen_dc": frozen_dc,
        "exceptions_pp": exceptions_pp,
        "exceptions_dc": exceptions_dc,
    }


@pytest.fixture
def valid() -> dict[str, Any]:
    return _build_valid()


def _dump(tmp_path: Path, name: str, data: Any) -> Path:
    path = tmp_path / name
    rp.write_json(path, data)
    return path


def _mutated(data: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    out = copy.deepcopy(data)
    out.update(overrides)
    return out


# ── positive controls ──────────────────────────────────────────────────────


def test_valid_corpus_loads(tmp_path: Path, valid: dict[str, Any]) -> None:
    path = _dump(tmp_path, "corpus.json", valid["corpus"])
    loaded = rp.load_corpus(path)
    assert loaded["count"] == valid["corpus"]["count"]


def test_valid_manifest_loads(tmp_path: Path, valid: dict[str, Any]) -> None:
    path = _dump(tmp_path, "manifest.json", valid["manifest"])
    loaded = rp.load_manifest(path)
    assert loaded["functions"]["parse_price"]["wired"] is True


def test_valid_frozen_loads(tmp_path: Path, valid: dict[str, Any]) -> None:
    manifest_path = _dump(tmp_path, "manifest.json", valid["manifest"])
    manifest = rp.load_manifest(manifest_path)
    path = _dump(tmp_path, "parse_price.frozen.json", valid["frozen_pp"])
    loaded = rp.load_frozen(path, manifest=manifest)
    assert loaded["function"] == "parse_price"


def test_valid_exceptions_loads(tmp_path: Path, valid: dict[str, Any]) -> None:
    manifest_path = _dump(tmp_path, "manifest.json", valid["manifest"])
    manifest = rp.load_manifest(manifest_path)
    frozen_path = _dump(tmp_path, "parse_price.frozen.json", valid["frozen_pp"])
    frozen = rp.load_frozen(frozen_path, manifest=manifest)
    path = _dump(tmp_path, "parse_price.exceptions.json", valid["exceptions_pp"])
    loaded = rp.load_exceptions(path, manifest=manifest, frozen=frozen)
    assert loaded["count"] == valid["exceptions_pp"]["count"]


# ── N1: truncation ──────────────────────────────────────────────────────────


def test_n1_truncated_corpus_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    path = _dump(tmp_path, "corpus.json", valid["corpus"])
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: len(text) // 2], encoding="utf-8")
    with pytest.raises(rp.ParityFormatError):
        rp.load_corpus(path)


# ── N2: rows: [] with count: 0 ──────────────────────────────────────────────


def test_n2_empty_rows_with_zero_count_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = _mutated(valid["frozen_pp"], rows=[], count=0)
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


# ── N3: count != len(rows) ───────────────────────────────────────────────────


def test_n3_count_mismatch_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = _mutated(valid["frozen_pp"], count=valid["frozen_pp"]["count"] + 1)
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


# ── N4: duplicate row ────────────────────────────────────────────────────────


def test_n4_duplicate_row_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    rows = copy.deepcopy(valid["frozen_pp"]["rows"])
    rows.append(copy.deepcopy(rows[-1]))
    bad = _mutated(valid["frozen_pp"], rows=rows, count=len(rows))
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


# ── N5: two rows swapped (unordered) ────────────────────────────────────────


def test_n5_unordered_rows_are_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    rows = copy.deepcopy(valid["frozen_pp"]["rows"])
    assert len(rows) >= 2
    rows[0], rows[1] = rows[1], rows[0]
    bad = _mutated(valid["frozen_pp"], rows=rows)
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


# ── N6/N7: input is not str|None ────────────────────────────────────────────


def test_n6_integer_input_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    rows = copy.deepcopy(valid["frozen_pp"]["rows"])
    rows[-1]["input"] = 12
    bad = _mutated(valid["frozen_pp"], rows=rows)
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


def test_n7_list_input_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    rows = copy.deepcopy(valid["frozen_pp"]["rows"])
    rows[-1]["input"] = ["12"]
    bad = _mutated(valid["frozen_pp"], rows=rows)
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


# ── N8: old is a float ───────────────────────────────────────────────────────


def test_n8_float_old_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    rows = copy.deepcopy(valid["frozen_pp"]["rows"])
    rows[-1]["old"] = 12.5
    bad = _mutated(valid["frozen_pp"], rows=rows)
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


# ── N9: old is a string but not a canonical value ───────────────────────────

PARSE_PRICE_BAD_OLD = ["1E+3", "NaN", "Infinity", "-5", "abc", ""]


@pytest.mark.parametrize("bad_value", PARSE_PRICE_BAD_OLD)
def test_n9_non_canonical_parse_price_old_is_rejected(
    tmp_path: Path, valid: dict[str, Any], bad_value: str
) -> None:
    rows = copy.deepcopy(valid["frozen_pp"]["rows"])
    non_none = [r for r in rows if r["old"] is not None]
    assert non_none
    non_none[0]["old"] = bad_value
    bad = _mutated(valid["frozen_pp"], rows=rows)
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


@pytest.mark.parametrize("bad_value", ["eur", "EURO"])
def test_n9_non_canonical_detect_currency_old_is_rejected(
    tmp_path: Path, valid: dict[str, Any], bad_value: str
) -> None:
    rows = copy.deepcopy(valid["frozen_dc"]["rows"])
    rows[-1]["old"] = bad_value
    bad = _mutated(valid["frozen_dc"], rows=rows)
    path = _dump(tmp_path, "detect_currency.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


# ── N10: extra key, at file level and at row level ──────────────────────────


def test_n10_extra_key_at_file_level_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = _mutated(valid["frozen_pp"], extra="unexpected")
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


def test_n10_extra_key_at_row_level_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    rows = copy.deepcopy(valid["frozen_pp"]["rows"])
    rows[-1]["extra"] = "unexpected"
    bad = _mutated(valid["frozen_pp"], rows=rows)
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


# ── N11: missing key (count, rows, old) ──────────────────────────────────────


def test_n11_missing_count_key_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = copy.deepcopy(valid["frozen_pp"])
    del bad["count"]
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


def test_n11_missing_rows_key_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = copy.deepcopy(valid["frozen_pp"])
    del bad["rows"]
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


def test_n11_missing_old_key_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    rows = copy.deepcopy(valid["frozen_pp"]["rows"])
    del rows[-1]["old"]
    bad = _mutated(valid["frozen_pp"], rows=rows)
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


# ── N12: schema: 2 ───────────────────────────────────────────────────────────


def test_n12_wrong_schema_version_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = _mutated(valid["corpus"], schema=2)
    path = _dump(tmp_path, "corpus.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_corpus(path)


# ── N13: isolated surrogate string ──────────────────────────────────────────


def test_n13_isolated_surrogate_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    path = _dump(tmp_path, "parse_price.frozen.json", valid["frozen_pp"])
    text = path.read_text(encoding="utf-8")
    # Inject a raw JSON escape for a lone surrogate: valid JSON source text (pure
    # ASCII bytes), but json.loads decodes it into a Python str with an unpaired
    # surrogate that cannot be re-encoded to UTF-8 (Sec. 3.4, P10).
    marker = '"$0.00"'
    assert marker in text
    text = text.replace(marker, '"\\ud800 12"', 1)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


# ── N14: exceptions row old diverges from the frozen file ──────────────────


def test_n14_exceptions_old_diverging_from_frozen_is_rejected(
    tmp_path: Path, valid: dict[str, Any]
) -> None:
    manifest_path = _dump(tmp_path, "manifest.json", valid["manifest"])
    manifest = rp.load_manifest(manifest_path)
    frozen_path = _dump(tmp_path, "parse_price.frozen.json", valid["frozen_pp"])
    frozen = rp.load_frozen(frozen_path, manifest=manifest)

    rows = copy.deepcopy(valid["exceptions_pp"]["rows"])
    rows[0]["old"] = "9999.00"
    bad = _mutated(valid["exceptions_pp"], rows=rows)
    path = _dump(tmp_path, "parse_price.exceptions.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_exceptions(path, manifest=manifest, frozen=frozen)


# ── N15: old == new ──────────────────────────────────────────────────────────


def test_n15_old_equals_new_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    manifest_path = _dump(tmp_path, "manifest.json", valid["manifest"])
    manifest = rp.load_manifest(manifest_path)
    frozen_path = _dump(tmp_path, "parse_price.frozen.json", valid["frozen_pp"])
    frozen = rp.load_frozen(frozen_path, manifest=manifest)

    rows = copy.deepcopy(valid["exceptions_pp"]["rows"])
    rows[0]["new"] = rows[0]["old"]
    bad = _mutated(valid["exceptions_pp"], rows=rows)
    path = _dump(tmp_path, "parse_price.exceptions.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_exceptions(path, manifest=manifest, frozen=frozen)


# ── N16: exceptions input outside the corpus (not in frozen) ────────────────


def test_n16_exceptions_input_outside_corpus_is_rejected(
    tmp_path: Path, valid: dict[str, Any]
) -> None:
    manifest_path = _dump(tmp_path, "manifest.json", valid["manifest"])
    manifest = rp.load_manifest(manifest_path)
    frozen_path = _dump(tmp_path, "parse_price.frozen.json", valid["frozen_pp"])
    frozen = rp.load_frozen(frozen_path, manifest=manifest)

    rows = copy.deepcopy(valid["exceptions_pp"]["rows"])
    rows.append(
        {
            "input": "not in the corpus at all",
            "old": "1.00",
            "new": None,
            "kind": "loss",
            "class": "residual_words",
            "note": "",
        }
    )
    rows.sort(key=lambda r: rp.sort_key(r["input"]))
    bad = _mutated(valid["exceptions_pp"], rows=rows, count=len(rows))
    path = _dump(tmp_path, "parse_price.exceptions.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_exceptions(path, manifest=manifest, frozen=frozen)


# ── N17: unknown class, and a class not admitted for that kind ─────────────


def test_n17_unknown_class_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    manifest_path = _dump(tmp_path, "manifest.json", valid["manifest"])
    manifest = rp.load_manifest(manifest_path)
    frozen_path = _dump(tmp_path, "parse_price.frozen.json", valid["frozen_pp"])
    frozen = rp.load_frozen(frozen_path, manifest=manifest)

    rows = copy.deepcopy(valid["exceptions_pp"]["rows"])
    rows[0]["class"] = "not_a_real_class"
    bad = _mutated(valid["exceptions_pp"], rows=rows)
    path = _dump(tmp_path, "parse_price.exceptions.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_exceptions(path, manifest=manifest, frozen=frozen)


def test_n17_class_not_admitted_for_kind_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    manifest_path = _dump(tmp_path, "manifest.json", valid["manifest"])
    manifest = rp.load_manifest(manifest_path)
    frozen_path = _dump(tmp_path, "parse_price.frozen.json", valid["frozen_pp"])
    frozen = rp.load_frozen(frozen_path, manifest=manifest)

    value_rows = [r for r in valid["exceptions_pp"]["rows"] if r["kind"] == "value"]
    assert value_rows, "the fixture must contain a 'value' row (kr. 89) to test this case"
    rows = copy.deepcopy(valid["exceptions_pp"]["rows"])
    for row in rows:
        if row["kind"] == "value":
            row["class"] = "zero"  # "zero" is a loss-only class
            break
    bad = _mutated(valid["exceptions_pp"], rows=rows)
    path = _dump(tmp_path, "parse_price.exceptions.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_exceptions(path, manifest=manifest, frozen=frozen)


# ── N18: class ending in _other requires a non-empty string note ───────────


def test_n18_other_class_with_empty_note_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    manifest_path = _dump(tmp_path, "manifest.json", valid["manifest"])
    manifest = rp.load_manifest(manifest_path)
    frozen_path = _dump(tmp_path, "parse_price.frozen.json", valid["frozen_pp"])
    frozen = rp.load_frozen(frozen_path, manifest=manifest)

    rows = copy.deepcopy(valid["exceptions_pp"]["rows"])
    loss_row = next(r for r in rows if r["kind"] == "loss")
    loss_row["class"] = "loss_other"
    loss_row["note"] = ""
    bad = _mutated(valid["exceptions_pp"], rows=rows)
    path = _dump(tmp_path, "parse_price.exceptions.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_exceptions(path, manifest=manifest, frozen=frozen)


def test_n18_other_class_with_non_string_note_is_rejected(
    tmp_path: Path, valid: dict[str, Any]
) -> None:
    manifest_path = _dump(tmp_path, "manifest.json", valid["manifest"])
    manifest = rp.load_manifest(manifest_path)
    frozen_path = _dump(tmp_path, "parse_price.frozen.json", valid["frozen_pp"])
    frozen = rp.load_frozen(frozen_path, manifest=manifest)

    rows = copy.deepcopy(valid["exceptions_pp"]["rows"])
    loss_row = next(r for r in rows if r["kind"] == "loss")
    loss_row["class"] = "loss_other"
    loss_row["note"] = 3
    bad = _mutated(valid["exceptions_pp"], rows=rows)
    path = _dump(tmp_path, "parse_price.exceptions.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_exceptions(path, manifest=manifest, frozen=frozen)


# ── N19: manifest subject unresolvable / not callable ───────────────────────


def test_n19_unresolvable_module_subject_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = copy.deepcopy(valid["manifest"])
    bad["functions"]["parse_price"]["subject"] = "price_tracker.core.nope:fn"
    path = _dump(tmp_path, "manifest.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_manifest(path)


def test_n19_not_callable_subject_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = copy.deepcopy(valid["manifest"])
    bad["functions"]["parse_price"]["subject"] = "price_tracker.core.scraper_base:USER_AGENTS"
    path = _dump(tmp_path, "manifest.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_manifest(path)


# ── N20: kind incoherent with old/new ───────────────────────────────────────


def test_n20_incoherent_kind_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    manifest_path = _dump(tmp_path, "manifest.json", valid["manifest"])
    manifest = rp.load_manifest(manifest_path)
    frozen_path = _dump(tmp_path, "parse_price.frozen.json", valid["frozen_pp"])
    frozen = rp.load_frozen(frozen_path, manifest=manifest)

    rows = copy.deepcopy(valid["exceptions_pp"]["rows"])
    loss_row = next(r for r in rows if r["kind"] == "loss")
    loss_row["kind"] = "gain"
    bad = _mutated(valid["exceptions_pp"], rows=rows)
    path = _dump(tmp_path, "parse_price.exceptions.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_exceptions(path, manifest=manifest, frozen=frozen)


# ── N21: corpus sections not disjoint / None in curated / non-reproducible ──


def test_n21_overlapping_sections_are_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = copy.deepcopy(valid["corpus"])
    dupe = bad["harvested"][-1]
    bad["curated"] = sorted([*bad["curated"], dupe], key=rp.sort_key)
    bad["count"] += 1
    path = _dump(tmp_path, "corpus.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_corpus(path)


def test_n21_none_in_curated_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = copy.deepcopy(valid["corpus"])
    bad["curated"] = [None, *bad["curated"]]
    bad["count"] += 1
    path = _dump(tmp_path, "corpus.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_corpus(path)


def test_n21_generated_not_reproducible_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = copy.deepcopy(valid["corpus"])
    bad["generated"]["inputs"] = sorted(
        [*bad["generated"]["inputs"], "not-from-the-generator"], key=rp.sort_key
    )
    bad["count"] += 1
    path = _dump(tmp_path, "corpus.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_corpus(path)


# ── N22: None twice ──────────────────────────────────────────────────────────


def test_n22_none_twice_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = copy.deepcopy(valid["corpus"])
    # A second literal null cannot be inserted losslessly through a Python list
    # (it would just dedupe): edit the raw JSON text instead.
    path = _dump(tmp_path, "corpus.json", bad)
    text = path.read_text(encoding="utf-8")
    text = text.replace('"harvested": [\n  null,', '"harvested": [\n  null,\n  null,', 1)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(rp.ParityFormatError):
        rp.load_corpus(path)


# ── N23: count with wrong scalar types ───────────────────────────────────────


def test_n23_float_count_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = _mutated(valid["frozen_pp"], count=float(valid["frozen_pp"]["count"]))
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


def test_n23_bool_count_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = _mutated(valid["frozen_pp"], count=True)
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


# ── N24: schema as a string ──────────────────────────────────────────────────


def test_n24_string_schema_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = _mutated(valid["corpus"], schema="1")
    path = _dump(tmp_path, "corpus.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_corpus(path)


# ── N25: wired as a non-bool ─────────────────────────────────────────────────


def test_n25_string_wired_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = copy.deepcopy(valid["manifest"])
    bad["functions"]["parse_price"]["wired"] = "true"
    path = _dump(tmp_path, "manifest.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_manifest(path)


def test_n25_int_wired_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = copy.deepcopy(valid["manifest"])
    bad["functions"]["parse_price"]["wired"] = 1
    path = _dump(tmp_path, "manifest.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_manifest(path)


# ── N26: rows as a non-list ──────────────────────────────────────────────────


def test_n26_dict_rows_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = _mutated(valid["frozen_pp"], rows={})
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


def test_n26_string_rows_is_rejected(tmp_path: Path, valid: dict[str, Any]) -> None:
    bad = _mutated(valid["frozen_pp"], rows="not a list")
    path = _dump(tmp_path, "parse_price.frozen.json", bad)
    with pytest.raises(rp.ParityFormatError):
        rp.load_frozen(path)


# ── N27/N28: the two class vocabularies do not cross ────────────────────────


def test_n27_parse_price_class_in_detect_currency_exceptions_is_rejected(
    tmp_path: Path, valid: dict[str, Any]
) -> None:
    manifest_path = _dump(tmp_path, "manifest.json", valid["manifest"])
    manifest = rp.load_manifest(manifest_path)
    frozen_path = _dump(tmp_path, "detect_currency.frozen.json", valid["frozen_dc"])
    frozen = rp.load_frozen(frozen_path, manifest=manifest)

    rows = copy.deepcopy(valid["exceptions_dc"]["rows"])
    rows[0]["class"] = "zero"  # a parse_price-only class
    bad = _mutated(valid["exceptions_dc"], rows=rows)
    path = _dump(tmp_path, "detect_currency.exceptions.json", bad)
    with pytest.raises(rp.ParityFormatError) as exc_info:
        rp.load_exceptions(path, manifest=manifest, frozen=frozen)
    assert exc_info.value.code == "exceptions_row_class_invalid"


def test_n28_detect_currency_class_in_parse_price_exceptions_is_rejected(
    tmp_path: Path, valid: dict[str, Any]
) -> None:
    manifest_path = _dump(tmp_path, "manifest.json", valid["manifest"])
    manifest = rp.load_manifest(manifest_path)
    frozen_path = _dump(tmp_path, "parse_price.frozen.json", valid["frozen_pp"])
    frozen = rp.load_frozen(frozen_path, manifest=manifest)

    rows = copy.deepcopy(valid["exceptions_pp"]["rows"])
    rows[0]["class"] = "shared_symbol"  # a detect_currency-only class
    bad = _mutated(valid["exceptions_pp"], rows=rows)
    path = _dump(tmp_path, "parse_price.exceptions.json", bad)
    with pytest.raises(rp.ParityFormatError) as exc_info:
        rp.load_exceptions(path, manifest=manifest, frozen=frozen)
    assert exc_info.value.code == "exceptions_row_class_invalid"


# ── N29-N31: cmd_exceptions' tolerant note reading, in a subprocess ─────────


def _write_cli_layout(root: Path, fn: str, frozen_rows: list[dict[str, Any]], subject: str) -> None:
    parity_dir = root / "tests" / "parity"
    parity_dir.mkdir(parents=True, exist_ok=True)
    other = "detect_currency" if fn == "parse_price" else "parse_price"
    manifest = {
        "schema": 1,
        "base_commit": "0" * 40,
        "functions": {
            fn: {
                "frozen": f"price_tracker.core.scraper_base:{fn}",
                "source_sha256": "0" * 64,
                "subject": subject,
                "wired": True,
            },
            other: {
                "frozen": f"price_tracker.core.scraper_base:{other}",
                "source_sha256": "0" * 64,
                "subject": None,
                "wired": False,
            },
        },
    }
    rp.write_json(parity_dir / "manifest.json", manifest)
    frozen = {"schema": 1, "function": fn, "count": len(frozen_rows), "rows": frozen_rows}
    rp.write_json(parity_dir / f"{fn}.frozen.json", frozen)


def test_n29_exceptions_write_over_mismatched_subject_keeps_notes(tmp_path: Path) -> None:
    _write_cli_layout(
        tmp_path,
        "parse_price",
        [{"input": "kr. 89", "old": "0.89"}],
        "price_tracker.core.scraper_base:parse_price",
    )
    parity_dir = tmp_path / "tests" / "parity"
    existing = {
        "schema": 1,
        "function": "parse_price",
        "subject": "price_tracker.core.scraper_base:some_other_subject",
        "count": 1,
        "rows": [
            {
                "input": "kr. 89",
                "old": "0.89",
                "new": "0.89",
                "kind": "value",
                "class": "token_with_dot",
                "note": "the note to keep",
            }
        ],
    }
    rp.write_json(parity_dir / "parse_price.exceptions.json", existing)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "exceptions",
            "parse_price",
            "--write",
            "--root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    written = json.loads((parity_dir / "parse_price.exceptions.json").read_text(encoding="utf-8"))
    rows = {r["input"]: r for r in written["rows"]}
    assert rows["kr. 89"]["note"] == "the note to keep"
    assert written["subject"] == "price_tracker.core.scraper_base:parse_price"


def test_n30_exceptions_write_over_rows_not_a_list_carries_no_notes(tmp_path: Path) -> None:
    _write_cli_layout(
        tmp_path,
        "parse_price",
        [{"input": "kr. 89", "old": "0.89"}],
        "price_tracker.core.scraper_base:parse_price",
    )
    parity_dir = tmp_path / "tests" / "parity"
    existing = {
        "schema": 1,
        "function": "parse_price",
        "subject": "price_tracker.core.scraper_base:parse_price",
        "count": 1,
        "rows": "not a list",
    }
    rp.write_json(parity_dir / "parse_price.exceptions.json", existing)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "exceptions",
            "parse_price",
            "--write",
            "--root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    written = json.loads((parity_dir / "parse_price.exceptions.json").read_text(encoding="utf-8"))
    assert written["rows"]
    assert all(row["note"] == "" for row in written["rows"])


def test_n31_exceptions_write_over_invalid_json_fails_loudly(tmp_path: Path) -> None:
    _write_cli_layout(
        tmp_path,
        "parse_price",
        [{"input": "kr. 89", "old": "0.89"}],
        "price_tracker.core.scraper_base:parse_price",
    )
    parity_dir = tmp_path / "tests" / "parity"
    (parity_dir / "parse_price.exceptions.json").write_text("{not json", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "exceptions",
            "parse_price",
            "--write",
            "--root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "JSONDecodeError" in result.stderr


# ── the harvest plugin, in a subprocess (never in this test's own process) ─


def test_harvest_plugin_records_every_binding(tmp_path: Path) -> None:
    synth_dir = tmp_path / "synth"
    synth_dir.mkdir()
    (synth_dir / "test_synth.py").write_text(
        "from price_tracker.scrapers import otto\n"
        "from price_tracker.core import scraper_base\n"
        "\n"
        "\n"
        "def test_calls_the_wrapped_functions() -> None:\n"
        '    otto.parse_price("1 €")\n'
        '    scraper_base.detect_currency("€")\n',
        encoding="utf-8",
    )
    out = tmp_path / "harvest_out.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "harvest",
            "--root",
            str(ROOT),
            "--tests",
            str(synth_dir),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "1 €" in data["parse_price"]
    assert "€" in data["detect_currency"]
    assert data["bindings"] >= 2
