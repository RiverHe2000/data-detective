"""Reproducible ranking evaluation on public/synthetic corpus findings.

The fixed relevance rubric below is authored before inference. It is not a user
study and does not establish correctness of free-text model interpretations.
Run with --model to measure real local Qwen calls, including cold-start latency.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data_detective.advisor import (  # noqa: E402
    DEFAULT_MODEL,
    PROMPT_VERSION,
    clear_recommendation_cache,
    finding_summaries,
    model_candidate_aliases,
    recommend,
    rules_recommend,
)
from data_detective.engine import analyze  # noqa: E402
from scripts.evaluate import load_case  # noqa: E402

# Freeze these prompts/labels before running Qwen; do not tune against test answers.
# Labels concern which deterministic rule's evidence to inspect, not business truth.
QUESTIONS = (
    ("dev-month", "dev", "Monthly sales coverage looks incomplete. What should I inspect?", ("date_parse",)),
    ("dev-duplicates", "dev", "Which duplicate records need receipt-level review before removal?", ("duplicate_rows",)),
    ("dev-missing", "dev", "Which missing fields should I verify against the source file?", ("missing_required",)),
    ("dev-numbers", "dev", "Some quantity or price values cannot be parsed. What should I inspect?", ("numeric_parse",)),
    ("dev-price", "dev", "Which unusual product prices might be legitimate special offers?", ("price_outlier",)),
    ("dev-coverage", "dev", "The monetary total omits records with unreadable values. What should I check?", ("numeric_parse", "missing_required")),
    ("test-month", "test", "Transactions show money but do not appear in a reporting period. Which leads matter?", ("date_parse",)),
    ("test-duplicates", "test", "A file may have been pasted into itself. Which suspect rows should I inspect?", ("duplicate_rows",)),
    ("test-missing", "test", "Some order identifiers were left blank. Which evidence can help me verify them?", ("missing_required",)),
    ("test-numbers", "test", "I suspect malformed quantities are excluding transactions from the total. Where should I start?", ("numeric_parse",)),
    ("test-price", "test", "An item has an unusually expensive sale. Which candidate should I verify before changing it?", ("price_outlier",)),
    ("test-coverage", "test", "Before trusting revenue, I want to investigate values that cannot be interpreted as money. Which lead first?", ("numeric_parse", "missing_required")),
)


def score(ids, findings, relevant_rules):
    by_id = {f.finding_id: f for f in findings}
    selected = [by_id[fid].rule_id for fid in ids if fid in by_id]
    return {"top_rule": selected[0] if selected else None,
            "top_priority_hit": bool(selected and selected[0] == relevant_rules[0]),
            "relevant_rule_recall_at_three": len(set(selected[:3]) & set(relevant_rules)) / len(relevant_rules)}


def versions():
    interpreter = os.environ.get("DATA_DETECTIVE_MODEL_PYTHON") or sys.executable
    code = "import importlib.metadata as m,json,sys; print(json.dumps({'python':sys.version,'torch':m.version('torch'),'transformers':m.version('transformers')}))"
    try:
        run = subprocess.run([interpreter, "-c", code], capture_output=True, text=True,
                             timeout=20, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        packages = json.loads(run.stdout) if run.returncode == 0 else {"unavailable": "GPU dependencies not installed in this interpreter"}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        packages = {"unavailable": "Could not inspect model interpreter"}
    return {"model_interpreter": interpreter, "packages": packages, "platform": platform.platform()}


def aggregate(calls, name):
    selected = [call[name] for call in calls if call.get(name) and call[name].get("accepted", True)]
    return {"scored_queries": len(selected),
            "top_priority_accuracy": statistics.mean(x["score"]["top_priority_hit"] for x in selected) if selected else None,
            "mean_relevant_rule_recall_at_three": statistics.mean(x["score"]["relevant_rule_recall_at_three"] for x in selected) if selected else None,
            "mean_elapsed_seconds": statistics.mean(x["result"]["elapsed_seconds"] for x in selected) if selected else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="store_true", help="Run real local Qwen sequentially; may take several minutes")
    parser.add_argument("--model-path", default=os.environ.get("DATA_DETECTIVE_MODEL_PATH", DEFAULT_MODEL))
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--limit", type=int, default=len(QUESTIONS))
    parser.add_argument("--questions", nargs="+", choices=[q[0] for q in QUESTIONS],
                        help="Run a named diagnostic subset; this is not a full evaluation")
    parser.add_argument("--repeat-cached", action="store_true", help="Measure an identical cached repeat after each accepted model call")
    parser.add_argument("--output", type=Path, help="Defaults to separate rule-only and model report files")
    args = parser.parse_args()
    version_suffix = PROMPT_VERSION.rsplit("-", 1)[-1]
    args.output = args.output or ROOT / "reports" / (f"advisor-evaluation-{version_suffix}.json" if args.model else f"advisor-rules-evaluation-{version_suffix}.json")
    if not 1 <= args.limit <= len(QUESTIONS):
        parser.error("--limit must be between one and the full query count")
    selected_questions = [q for q in QUESTIONS if args.questions is None or q[0] in args.questions][:args.limit]
    clear_recommendation_cache()
    corpus, corpus_hashes = {}, {}
    for split in ("dev", "test"):
        combined = []
        for case_dir in sorted((ROOT / "data" / "cases").glob(f"{split}-*")):
            version, _ = load_case(case_dir)
            corpus_hashes[case_dir.name] = hashlib.sha256((case_dir / "input.csv").read_bytes()).hexdigest()
            combined.extend(replace(f, finding_id=f"{case_dir.name}::{f.finding_id}")
                            for f in analyze(version).findings)
        if not combined:
            raise RuntimeError(f"No corpus findings for {split}; run the corpus builder first")
        corpus[split] = combined
    report = {
        "generated_at": datetime.now(UTC).isoformat(), "prompt_version": PROMPT_VERSION,
        "evaluation_kind": "diagnostic smoke subset" if len(selected_questions) < len(QUESTIONS) else "full reused question set",
        "model_input_policy": "Bounded rule summaries without raw amounts, dates or counts; short aliases map to actual finding IDs",
        "question_set_sha256": hashlib.sha256(json.dumps(QUESTIONS, ensure_ascii=False).encode()).hexdigest(),
        "label_method": "Predefined rule-relevance rubric frozen before inference; no human interpretation-accuracy claim",
        "limitations": ["Small public/synthetic development and held-out corpus; not a production accuracy estimate",
                        "Prompt and request handling have engineering corrections informed by earlier failures; these are reused questions, not a fresh untouched holdout",
                        "Protocol validation does not prove explanation correctness; no human semantic ratings collected",
                        "Cold-process latency includes Python startup, model loading, generation and validation",
                        "Failed model calls use rules in the product, but are not scored as successful model answers"],
        "model_requested": args.model, "model_path": args.model_path if args.model else None,
        "environment": versions() if args.model else {"python": sys.version, "platform": platform.platform()},
        "corpus_sha256": corpus_hashes, "questions": [],
    }
    config = Path(args.model_path) / "config.json"
    if args.model and config.is_file():
        report["model_config_sha256"] = hashlib.sha256(config.read_bytes()).hexdigest()
    for qid, split, question, relevant in selected_questions:
        findings = corpus[split]
        baseline = rules_recommend(question, findings)
        candidates = finding_summaries(question, findings)
        offered = {f["rule_id"] for f in candidates}
        call = {"question_id": qid, "split": split, "question": question,
                "relevant_rule_ids_in_priority_order": list(relevant),
                "corpus_finding_count": len(findings), "model_candidates": candidates,
                "model_candidate_aliases": model_candidate_aliases(candidates),
                "rubric_rules_available_to_model": set(relevant).issubset(offered),
                "rules": {"result": asdict(baseline), "score": score(baseline.finding_ids, findings, relevant)}}
        if args.model:
            result = recommend(question, findings, args.model_path, args.timeout, use_cache=args.repeat_cached)
            call["qwen"] = {"accepted": result.mode == "qwen", "result": asdict(result),
                            "score": score(result.finding_ids, findings, relevant)}
            print(f"{qid}: {result.mode}, {result.elapsed_seconds:.2f}s, {result.fallback_reason or 'valid protocol'}", flush=True)
            if args.repeat_cached and result.mode == "qwen":
                repeated = recommend(question, findings, args.model_path, args.timeout)
                call["cached_repeat"] = {"result": asdict(repeated),
                                         "cache_hit": repeated.mode == "qwen_cached",
                                         "same_advice": (result.finding_ids, result.explanation, result.next_steps) ==
                                                        (repeated.finding_ids, repeated.explanation, repeated.next_steps)}
        report["questions"].append(call)
        # Preserve every completed generation if a later run is interrupted.
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    calls = report["questions"]
    report["summary"] = {"rules": aggregate(calls, "rules"), "qwen_accepted_only": aggregate(calls, "qwen")}
    if args.model:
        failures = [call["qwen"]["result"]["fallback_reason"] for call in calls if not call["qwen"]["accepted"]]
        durations = [call["qwen"]["result"]["elapsed_seconds"] for call in calls]
        effective = [{"effective": {"result": call["qwen"]["result"], "score": call["qwen"]["score"]}}
                     for call in calls]
        report["summary"].update({"attempted_model_calls": len(calls), "fallback_count": len(failures),
                                  "protocol_acceptance_rate": (len(calls) - len(failures)) / len(calls),
                                  "product_with_fallback": aggregate(effective, "effective"),
                                  "failure_categories": {key: sum(reason.startswith(key) for reason in failures)
                                                         for key in ("schema", "citation", "numeric_claim", "unsupported_assertion", "timeout", "worker_failure", "unavailable")},
                                  "cold_call_median_seconds": statistics.median(durations),
                                  "cold_call_max_seconds": max(durations)})
        repeats = [call["cached_repeat"] for call in calls if "cached_repeat" in call]
        if repeats:
            report["summary"]["cached_repeats"] = {
                "count": len(repeats), "all_cache_hits": all(r["cache_hit"] for r in repeats),
                "all_advice_equal": all(r["same_advice"] for r in repeats),
                "median_seconds": statistics.median(r["result"]["elapsed_seconds"] for r in repeats),
            }
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
