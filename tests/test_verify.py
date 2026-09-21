"""The verification entry point must not turn incomplete/failed evidence into a pass."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from scripts.verify import case_gate, changed_files, junit_counts
from scripts.verify import tests_passed as passed_tests

ROOT = Path(__file__).resolve().parents[1]


def known_report():
    return json.loads((ROOT / "reports/evaluation.json").read_text(encoding="utf-8"))


def test_case_gate_rejects_regression_even_when_evaluator_exit_would_be_zero():
    report = known_report()
    assert case_gate(report)["passed"]
    report["summary"]["test"]["injections"]["copied_records"]["detected"] -= 1
    result = case_gate(report)
    assert not result["passed"]
    assert any("missed known injection events" in message for message in result["failures"])


def test_case_gate_allows_documented_review_but_rejects_error_flags():
    report = known_report()
    assert report["summary"]["all"]["controls"]["legitimate_repeat"]["review_rows"] == 24
    assert case_gate(report)["passed"]
    changed = copy.deepcopy(report)
    changed["summary"]["all"]["controls"]["legitimate_repeat"]["error_rows"] = 1
    assert not case_gate(changed)["passed"]


def test_case_gate_rejects_missing_evidence():
    assert not case_gate({})["passed"]
    report = known_report()
    report["summary"]["test"]["cases"] = 0
    assert not case_gate(report)["passed"]


def test_source_changes_include_added_deleted_and_changed_files():
    assert changed_files(
        {"same": "a", "deleted": "b", "edited": "c"}, {"same": "a", "added": "d", "edited": "e"}
    ) == ["added", "deleted", "edited"]


def test_junit_counts_includes_failures_and_skips(tmp_path):
    path = tmp_path / "results.xml"
    path.write_text('<testsuites><testsuite tests="4" failures="1" errors="0" skipped="1"/></testsuites>')
    assert junit_counts(path) == {"tests": 4, "failures": 1, "errors": 0, "skipped": 1}
    assert junit_counts(tmp_path / "missing.xml") is None


def test_no_test_or_all_skipped_is_not_a_pass():
    assert not passed_tests(None)
    assert not passed_tests({"tests": 0, "failures": 0, "errors": 0, "skipped": 0})
    assert not passed_tests({"tests": 4, "failures": 0, "errors": 0, "skipped": 4})
    assert passed_tests({"tests": 4, "failures": 0, "errors": 0, "skipped": 1})
