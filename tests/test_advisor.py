from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from data_detective import advisor
from data_detective.models import Finding


@pytest.fixture(autouse=True)
def empty_advice_cache():
    advisor.clear_recommendation_cache()
    yield
    advisor.clear_recommendation_cache()


def finding(fid="F-1", rule="numeric_parse", evidence=None):
    return Finding(fid, rule, "error", "Check source values", ["row-1"], "Quantity",
                   evidence or {}, "Inspect the source value under the confirmed format.")


def valid_response(**overrides):
    value = {"finding_ids": ["F-1"], "explanation": "These values may affect coverage.",
             "next_steps": ["Inspect the evidence and compare a repair preview."]}
    value.update(overrides)
    return json.dumps(value)


def test_rules_are_deterministic_and_question_sensitive():
    findings = [finding(), finding("F-2", "date_parse"), finding("F-3", "duplicate_rows")]
    a = advisor.rules_recommend("Investigate monthly sales", findings)
    b = advisor.rules_recommend("Investigate monthly sales", list(reversed(findings)))
    assert a.finding_ids == b.finding_ids
    assert a.finding_ids[0] == "F-2"
    assert advisor.rules_recommend("检查重复记录", findings).finding_ids[0] == "F-3"


def test_rules_use_word_boundaries_and_specific_parse_intent():
    findings = [finding(), finding("F-date", "date_parse"), finding("F-price", "price_outlier")]
    assert advisor.rules_recommend("Review this expensive candidate", findings).finding_ids[0] == "F-price"
    assert advisor.rules_recommend("Unit prices cannot be parsed", findings).finding_ids[0] == "F-1"
    assert advisor.rules_recommend("检查单价解析格式", findings).finding_ids[0] == "F-1"


def test_no_findings_never_loads_model(monkeypatch):
    monkeypatch.setattr(advisor, "_run_worker", lambda *a, **kw: pytest.fail("must not spawn"))
    result = advisor.recommend("Review", [])
    assert result.mode == "rules"
    assert result.finding_ids == []
    assert "does not prove" in result.explanation


@pytest.mark.parametrize("raw,category", [
    ("not json", "schema"),
    ("[]", "schema"),
    (valid_response(extra="unexpected"), "schema"),
    (valid_response(finding_ids=["F-404"]), "citation"),
    (valid_response(finding_ids=["F-1", "F-1"]), "citation"),
    (valid_response(finding_ids=[]), "schema"),
    (valid_response(finding_ids=[True]), "schema"),
    (valid_response(next_steps="delete"), "schema"),
    (valid_response(next_steps=[""]), "schema"),
    (valid_response(explanation="Sales increased by 42 percent."), "numeric_claim"),
    (valid_response(explanation="There are two copies."), "numeric_claim"),
    (valid_response(explanation="Affected amount is £lots."), "numeric_claim"),
    (valid_response(explanation="约有三条重复记录。"), "numeric_claim"),
    (valid_response(explanation="This is proven fraud."), "unsupported_assertion"),
    (valid_response(next_steps=["Automatically delete the duplicates."]), "unsupported_assertion"),
])
def test_invalid_model_contract_is_rejected(raw, category):
    with pytest.raises(ValueError, match=category):
        advisor.validate_response(raw, {"F-1"})


def test_valid_selection_is_only_cited_in_id_array():
    result = advisor.validate_response(valid_response(), {"F-1"})
    assert result["finding_ids"] == ["F-1"]
    with pytest.raises(ValueError, match="citation"):
        advisor.validate_response(valid_response(explanation="Inspect finding-alpha."), {"F-1", "finding-alpha"})


def test_prompt_is_bounded_and_never_contains_entire_row_table():
    summary = advisor.finding_summaries("x" * 5000, [
        finding(f"F-{i}", evidence={"sample": "untrusted " * 1000}) for i in range(40)
    ])
    assert len(summary) == 20
    assert all("row_ids" not in item and "rows" not in item for item in summary)
    assert "untrusted" not in json.dumps(summary)


def test_ranker_gets_check_meaning_without_raw_literals_or_row_hashes():
    row_hash = "a" * 64
    summary = advisor.finding_summaries("Review dates", [finding(evidence={
        "examples": [{"row_id": row_hash, "raw": "2011-02-30"}], "rows": [row_hash],
    })])
    assert summary[0]["rule_id"] == "numeric_parse"
    assert "2011-02-30" not in json.dumps(summary)
    assert row_hash not in json.dumps(summary)


def test_candidate_budget_preserves_less_frequent_rule_types():
    findings = [finding(f"dup-{i:03}", "duplicate_rows") for i in range(50)]
    findings += [finding("rare-price", "price_outlier"), finding("rare-date", "date_parse")]
    summaries = advisor.finding_summaries("Review duplicate rows", findings)
    assert len(summaries) == advisor.MAX_FINDINGS
    assert {x["rule_id"] for x in summaries} == {"duplicate_rows", "price_outlier", "date_parse"}


def test_missing_model_keeps_functional_rules(tmp_path):
    result = advisor.recommend("Review", [finding()], str(tmp_path / "missing"))
    assert result.mode == "fallback"
    assert result.finding_ids == ["F-1"]
    assert "unavailable" in result.fallback_reason


def test_success_uses_isolated_local_worker_and_overridden_interpreter(tmp_path, monkeypatch):
    observed = {}

    def fake_run(command, **kwargs):
        observed.update(command=command, kwargs=kwargs)
        request = json.loads(Path(command[-2]).read_text(encoding="utf-8"))
        assert len(request["question"]) == 1000
        assert request["findings"][0]["finding_id"] == "F_A"
        Path(command[-1]).write_text(json.dumps({"ok": True, "raw_response": valid_response(finding_ids=["F_A"])}), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("DATA_DETECTIVE_MODEL_PYTHON", "gpu-python")
    monkeypatch.setattr(advisor, "_run_worker", fake_run)
    result = advisor.recommend("x" * 1500, [finding()], str(tmp_path), timeout_seconds=3)
    assert result.mode == "qwen"
    assert result.raw_response == valid_response(finding_ids=["F_A"])
    assert result.finding_ids == ["F-1"]
    assert observed["command"][:3] == ["gpu-python", "-m", "data_detective.model_worker"]
    assert 0 < observed["kwargs"]["timeout"] <= 3
    assert observed["kwargs"]["env"]["HF_HUB_OFFLINE"] == "1"
    assert not Path(observed["command"][-2]).exists()


def test_timeout_returns_rules_and_removes_private_request(tmp_path, monkeypatch):
    observed = []

    def fake_run(command, **kwargs):
        observed.append(command[-2])
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(advisor, "_run_worker", fake_run)
    result = advisor.recommend("Review", [finding()], str(tmp_path), timeout_seconds=0.01)
    assert result.mode == "fallback"
    assert result.fallback_reason.startswith("timeout:")
    assert result.finding_ids == ["F-1"]
    assert not Path(observed[0]).exists()


def test_model_failure_keeps_raw_invalid_generation(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        Path(command[-1]).write_text(json.dumps({"ok": True, "raw_response": valid_response(finding_ids=["FAKE"])}),
                                     encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(advisor, "_run_worker", fake_run)
    result = advisor.recommend("Review", [finding()], str(tmp_path))
    assert result.mode == "fallback"
    assert result.fallback_reason.startswith("citation:")
    assert "FAKE" in result.raw_response
    assert result.finding_ids == ["F-1"]


def test_missing_result_is_actionable_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(advisor, "_run_worker", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    result = advisor.recommend("Review", [finding()], str(tmp_path))
    assert result.mode == "fallback"
    assert "worker_failure" in result.fallback_reason


@pytest.mark.parametrize("worker_result", [[], {"ok": True, "raw_response": 42},
                                          {"ok": False, "error": "CUDA unavailable"}])
def test_bad_worker_payload_falls_back(tmp_path, monkeypatch, worker_result):
    def fake_run(command, **kwargs):
        Path(command[-1]).write_text(json.dumps(worker_result), encoding="utf-8")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(advisor, "_run_worker", fake_run)
    result = advisor.recommend("Review", [finding()], str(tmp_path))
    assert result.mode == "fallback"
    assert result.finding_ids == ["F-1"]
    assert result.raw_response is None


def test_citations_must_be_in_offered_subset_not_only_original_findings(tmp_path, monkeypatch):
    findings = [finding(f"F-{i:03}") for i in range(21)]

    def fake_run(command, **kwargs):
        request = json.loads(Path(command[-2]).read_text(encoding="utf-8"))
        assert "F_U" not in {f["finding_id"] for f in request["findings"]}
        Path(command[-1]).write_text(json.dumps({"ok": True, "raw_response": valid_response(finding_ids=["F_U"])}),
                                     encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(advisor, "_run_worker", fake_run)
    result = advisor.recommend("Review", findings, str(tmp_path))
    assert result.mode == "fallback"
    assert result.fallback_reason.startswith("citation:")


def successful_worker(counter, delay=0):
    def fake_run(command, **kwargs):
        counter.append(command)
        if delay:
            time.sleep(delay)
        Path(command[-1]).write_text(json.dumps({"ok": True, "raw_response": valid_response(finding_ids=["F_A"])}),
                                     encoding="utf-8")
        return SimpleNamespace(returncode=0)
    return fake_run


def test_identical_advice_is_cached_with_current_latency_and_fresh_lists(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(advisor, "_run_worker", successful_worker(calls, delay=0.01))
    first = advisor.recommend("Review", [finding()], str(tmp_path))
    first.finding_ids.append("mutated-by-caller")
    started = time.perf_counter()
    second = advisor.recommend("Review", [finding()], str(tmp_path))
    lookup_wall_seconds = time.perf_counter() - started
    assert first.mode == "qwen"
    assert second.mode == "qwen_cached"
    assert second.finding_ids == ["F-1"]
    assert 0 <= second.elapsed_seconds <= lookup_wall_seconds
    assert len(calls) == 1


def test_cache_invalidates_for_question_ids_model_files_and_interpreter(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(advisor, "_run_worker", successful_worker(calls))
    assert advisor.recommend("Review", [finding()], str(tmp_path)).mode == "qwen"
    assert advisor.recommend("Check", [finding()], str(tmp_path)).mode == "qwen"
    assert advisor.recommend("Review", [finding("F-other")], str(tmp_path)).mode == "qwen"
    (tmp_path / "config.json").write_text('{"updated": true}', encoding="utf-8")
    assert advisor.recommend("Review", [finding()], str(tmp_path)).mode == "qwen"
    monkeypatch.setenv("DATA_DETECTIVE_MODEL_PYTHON", "another-interpreter")
    assert advisor.recommend("Review", [finding()], str(tmp_path)).mode == "qwen"
    assert len(calls) == 5


def test_cache_is_bounded_expires_and_can_be_bypassed(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(advisor, "_run_worker", successful_worker(calls))
    monkeypatch.setattr(advisor, "_CACHE_MAX_ENTRIES", 2)
    for question in ("alpha", "beta", "gamma", "alpha"):
        assert advisor.recommend(question, [finding()], str(tmp_path)).mode == "qwen"
    assert len(advisor._RESPONSE_CACHE) == 2
    assert advisor.recommend("alpha", [finding()], str(tmp_path), use_cache=False).mode == "qwen"
    advisor.clear_recommendation_cache()
    monkeypatch.setattr(advisor, "_CACHE_TTL_SECONDS", 0)
    assert advisor.recommend("expire", [finding()], str(tmp_path)).mode == "qwen"
    assert advisor.recommend("expire", [finding()], str(tmp_path)).mode == "qwen"
    assert len(calls) == 7


def test_failures_are_not_cached(tmp_path, monkeypatch):
    calls = []
    success = successful_worker(calls)

    def first_fails(command, **kwargs):
        if not calls:
            calls.append(command)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return success(command, **kwargs)

    monkeypatch.setattr(advisor, "_run_worker", first_fails)
    assert advisor.recommend("Review", [finding()], str(tmp_path)).mode == "fallback"
    assert advisor.recommend("Review", [finding()], str(tmp_path)).mode == "qwen"
    assert advisor.recommend("Review", [finding()], str(tmp_path)).mode == "qwen_cached"
    assert len(calls) == 2


def test_concurrent_identical_requests_share_validated_result(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(advisor, "_run_worker", successful_worker(calls, delay=0.1))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: advisor.recommend("Review", [finding()], str(tmp_path)), range(2)))
    assert sorted(r.mode for r in results) == ["qwen", "qwen_cached"]
    assert len(calls) == 1


def test_busy_worker_respects_callers_total_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(advisor, "_run_worker", lambda *a, **kw: pytest.fail("must not spawn"))
    advisor._MODEL_LOCK.acquire()
    try:
        result = advisor.recommend("Review", [finding()], str(tmp_path), timeout_seconds=0.01)
        assert result.mode == "fallback"
        assert "active local model" in result.fallback_reason
    finally:
        advisor._MODEL_LOCK.release()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 301, True])
def test_invalid_timeout_does_not_spawn(timeout, tmp_path, monkeypatch):
    monkeypatch.setattr(advisor, "_run_worker", lambda *a, **kw: pytest.fail("must not spawn"))
    result = advisor.recommend("Review", [finding()], str(tmp_path), timeout)
    assert result.mode == "fallback"
    assert result.fallback_reason.startswith("configuration:")


def test_invalid_path_does_not_crash_rules_workflow():
    result = advisor.recommend("Review", [finding()], model_path=None)
    assert result.mode == "fallback"
    assert result.fallback_reason.startswith("configuration:")


@pytest.mark.skipif(os.name != "nt", reason="Regression for Windows venv launcher process trees")
def test_actual_timeout_terminates_windows_descendant(tmp_path):
    marker = tmp_path / "descendant.txt"
    child_code = "import time; time.sleep(60)"
    parent_code = (
        "import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
        "Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        advisor._run_worker([sys.executable, "-c", parent_code, str(marker), child_code],
                            timeout=2, env=os.environ.copy())
    assert marker.is_file(), "The descendant must have started for this regression test"
    child_pid = int(marker.read_text())
    process_list = subprocess.run(["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
                                  capture_output=True, check=True, timeout=10,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
    assert f'"{child_pid}"'.encode("ascii") not in process_list.stdout
