"""The benchmark's provenance and answers must not depend on detector behavior."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_cases import make_case  # noqa: E402
from scripts.evaluate import evaluate_case, independent_financials, load_case  # noqa: E402

CASES = ROOT / "data" / "cases"
MANIFESTS = sorted(CASES.glob("*/manifest.json"))


def test_twelve_frozen_cases_and_complete_lock():
    assert len(MANIFESTS) == 12
    lock = json.loads((CASES / "corpus.lock.json").read_text())
    assert len(lock["files"]) == 36
    for name, expected in lock["files"].items():
        assert hashlib.sha256((CASES / name).read_bytes()).hexdigest() == expected


def test_splits_have_no_original_source_overlap():
    seen: set[int] = set()
    splits = Counter()
    for path in MANIFESTS:
        manifest = json.loads(path.read_text())
        splits[manifest["split"]] += 1
        rows = [item["source_data_row"] for item in manifest["row_provenance"] if item["origin"] == "uci"]
        assert len(rows) == 120
        assert rows == list(range(rows[0], rows[0] + 120))
        assert not (seen & set(rows))
        seen.update(rows)
    assert splits == {"dev": 6, "test": 6}


@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=lambda path: path.parent.name)
def test_case_can_be_rebuilt_byte_for_byte_from_its_recorded_source_rows(manifest_path):
    manifest = json.loads(manifest_path.read_text())
    baseline = list(csv.DictReader(io.StringIO((manifest_path.parent / "baseline.csv").read_text())))
    index = (manifest["source_window"]["first_data_row"] - 1) // 40_000
    rebuilt = make_case(index, manifest["columns"], baseline[:120], manifest["source"])
    for name, content in rebuilt.items():
        assert (CASES / name).read_bytes() == content


@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=lambda path: path.parent.name)
def test_inverse_edits_restore_exact_baseline_without_using_engine(manifest_path):
    manifest = json.loads(manifest_path.read_text())
    baseline = list(csv.DictReader(io.StringIO((manifest_path.parent / "baseline.csv").read_text())))
    rows = list(csv.DictReader(io.StringIO((manifest_path.parent / "input.csv").read_text())))
    excluded = set()
    for operation in manifest["oracle_repairs"]:
        if operation["kind"] == "set_cell":
            for number in operation["row_indices"]:
                rows[number - 1][operation["column"]] = operation["value"]
        else:
            assert operation["kind"] == "exclude_rows"
            excluded.update(operation["row_indices"])
    assert [row for i, row in enumerate(rows, start=1) if i not in excluded] == baseline
    assert len(manifest["row_provenance"]) == len(rows)
    assert len([item for item in manifest["row_provenance"] if item["origin"] == "synthetic_control"]) == 16


def test_independent_oracle_handles_returns_and_unassigned_amount():
    content = b"Quantity,UnitPrice,InvoiceDate\n2,0.1,2011-01-01 00:00:00\n-1,0.1,2011-01-02 00:00:00\n3,2,bad\nfoo,4,2011-02-01 00:00:00\n"
    result = independent_financials(content)
    assert result == dict(
        net_amount="6.1",
        monthly={"2011-01": "0.1"},
        invalid_amount_rows=1,
        invalid_date_rows=1,
        unassigned_amount="6",
    )


def test_evaluator_uses_labels_instead_of_detector_as_truth(monkeypatch):
    from data_detective import engine

    original = engine.analyze
    monkeypatch.setattr(engine, "analyze", lambda version: replace(original(version), findings=[]))
    result = evaluate_case(CASES / "test-mixed-incident")
    assert len(result["injections"]) == 8
    assert all(not item["detected"] for item in result["injections"])
    assert result["oracle_restores_baseline_rows"]


def test_permitted_duplicate_review_is_not_an_error_false_positive():
    result = evaluate_case(CASES / "dev-duplicate-dispatch")
    control = next(item for item in result["controls"] if item["kind"] == "legitimate_repeat")
    assert len(control["review_rows"]) == 2
    assert control["error_rows"] == []
    assert control["unexpected_review_rows"] == []


def test_load_case_rejects_changed_input(tmp_path):
    source = CASES / "test-broken-dates"
    (tmp_path / "manifest.json").write_bytes((source / "manifest.json").read_bytes())
    (tmp_path / "input.csv").write_bytes((source / "input.csv").read_bytes() + b"\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_case(tmp_path)
