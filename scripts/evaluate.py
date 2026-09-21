"""Evaluate frozen injection labels and stipulated controls, not presumed-clean retail data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_detective.models import (  # noqa: E402
    ColumnMap,
    DatasetVersion,
    ParseSettings,
    RepairOperation,
    RepairPlan,
)


def load_case(case_dir: Path, filename: str = "input.csv") -> tuple[DatasetVersion, dict]:
    from data_detective.engine import read_csv

    case_dir = Path(case_dir)
    manifest = json.loads((case_dir / "manifest.json").read_text(encoding="utf-8"))
    content = (case_dir / filename).read_bytes()
    key = "input_sha256" if filename == "input.csv" else "baseline_sha256"
    if hashlib.sha256(content).hexdigest() != manifest[key]:
        raise ValueError(f"Frozen case hash mismatch: {manifest['case_id']}/{filename}")
    columns, rows = read_csv(content)
    version = DatasetVersion(
        version_id=f"{manifest['case_id']}:{filename}",
        dataset_id=manifest["case_id"],
        parent_version_id=None,
        columns=columns,
        rows=rows,
        mapping=ColumnMap(**manifest["mapping"]),
        settings=ParseSettings(**manifest["settings"]),
        description=manifest["truth_statement"],
    )
    return version, manifest


def independent_financials(content: bytes) -> dict:
    """Small Decimal oracle for our fixed ISO/dot-decimal fixture contract, not engine code."""
    total = Decimal(0)
    monthly: dict[str, Decimal] = defaultdict(Decimal)
    invalid_amount = invalid_date = 0
    unassigned = Decimal(0)
    for row in csv.DictReader(io.StringIO(content.decode("utf-8"))):
        try:
            amount = Decimal(row["Quantity"]) * Decimal(row["UnitPrice"])
            if not amount.is_finite():
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            invalid_amount += 1
            amount = None
        try:
            month = datetime.strptime(row["InvoiceDate"], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m")
        except ValueError:
            invalid_date += 1
            month = None
        if amount is not None:
            total += amount
            if month is None:
                unassigned += amount
            else:
                monthly[month] += amount
    return dict(
        net_amount=str(total),
        monthly={key: str(value) for key, value in sorted(monthly.items())},
        invalid_amount_rows=invalid_amount,
        invalid_date_rows=invalid_date,
        unassigned_amount=str(unassigned),
    )


def indexed_findings(analysis, version: DatasetVersion) -> list[dict]:
    indexes = {row.row_id: i for i, row in enumerate(version.rows, start=1)}
    return [
        dict(
            rule_id=finding.rule_id,
            severity=finding.severity,
            column=finding.column,
            row_indices=[indexes[row_id] for row_id in finding.row_ids],
        )
        for finding in analysis.findings
    ]


def financials_match(metrics, expected: dict) -> bool:
    return (
        Decimal(metrics.net_amount) == Decimal(expected["net_amount"])
        and Decimal(metrics.unassigned_amount) == Decimal(expected["unassigned_amount"])
        and metrics.invalid_amount_rows == expected["invalid_amount_rows"]
        and metrics.invalid_date_rows == expected["invalid_date_rows"]
        and {key: Decimal(value) for key, value in metrics.monthly.items()}
        == {key: Decimal(value) for key, value in expected["monthly"].items()}
    )


def evaluate_case(case_dir: Path) -> dict:
    from data_detective.engine import analyze, preview_repair

    version, manifest = load_case(case_dir)
    baseline, _ = load_case(case_dir, "baseline.csv")
    actual = analyze(version)
    reference = analyze(baseline)
    detected = indexed_findings(actual, version)
    existing = indexed_findings(reference, baseline)
    injection_scores = []
    for injection in manifest["injections"]:
        targets = set(injection["input_row_indices"])
        columns = {diff["column"] for diff in injection["cell_diff"] if "column" in diff}

        def hits(
            findings: list[dict],
            require_severity: bool,
            annotation=injection,
            wanted_columns=columns,
            wanted_rows=targets,
        ) -> set[int]:
            return {
                number
                for finding in findings
                if finding["rule_id"] == annotation["rule_id"]
                and (not require_severity or finding["severity"] == annotation["expected_severity"])
                and (not wanted_columns or finding["column"] in wanted_columns)
                for number in finding["row_indices"]
                if number in wanted_rows
            }

        found = hits(detected, True)
        previous = hits(existing, False)
        injection_scores.append(
            dict(
                kind=injection["kind"],
                expected_rule=injection["rule_id"],
                expected_severity=injection["expected_severity"],
                targets=sorted(targets),
                detected_rows=sorted(found),
                detected=found == targets,
                pre_existing_same_rule_rows=sorted(previous),
                incremental_detected_rows=sorted(found - previous),
            )
        )
    control_scores = []
    for control in manifest["controls"]:
        targets = set(control["input_row_indices"])
        errors = {
            i
            for finding in detected
            if finding["severity"] == "error"
            for i in finding["row_indices"]
            if i in targets
        }
        reviews = {
            i
            for finding in detected
            if finding["severity"] == "review"
            for i in finding["row_indices"]
            if i in targets
        }
        unexpected = {
            i
            for finding in detected
            if finding["severity"] == "review" and finding["rule_id"] not in control["allowed_review_rules"]
            for i in finding["row_indices"]
            if i in targets
        }
        control_scores.append(
            dict(
                kind=control["kind"],
                n_rows=len(targets),
                error_rows=sorted(errors),
                review_rows=sorted(reviews),
                unexpected_review_rows=sorted(unexpected),
                allowed_review_rules=control["allowed_review_rules"],
            )
        )
    operations = [
        RepairOperation(
            kind=operation["kind"],
            row_ids=[version.rows[i - 1].row_id for i in operation["row_indices"]],
            column=operation.get("column"),
            value=operation.get("value"),
        )
        for operation in manifest["oracle_repairs"]
    ]
    plan = RepairPlan(
        version.version_id,
        operations,
        "Known-injection oracle restoration; not an automatic repair recommendation",
    )
    preview = preview_repair(version, plan)
    restored = [row.values for row in preview.version.rows if not row.excluded]
    expected_rows = [row.values for row in baseline.rows]
    independent = independent_financials((case_dir / "baseline.csv").read_bytes())
    input_independent = independent_financials((case_dir / "input.csv").read_bytes())
    return dict(
        case_id=manifest["case_id"],
        split=manifest["split"],
        injections=injection_scores,
        controls=control_scores,
        baseline_finding_counts=dict(Counter(item["rule_id"] for item in existing)),
        input_finding_counts=dict(Counter(item["rule_id"] for item in detected)),
        oracle_restores_baseline_rows=restored == expected_rows,
        baseline_metrics_match_independent_oracle=financials_match(reference.metrics, independent),
        input_metrics_match_independent_oracle=financials_match(actual.metrics, input_independent),
        restored_metrics_match_independent_oracle=financials_match(preview.after.metrics, independent),
        expected_baseline_financials=independent,
        baseline_metrics=reference.metrics.to_dict(),
        input_metrics=actual.metrics.to_dict(),
        restored_metrics=preview.after.metrics.to_dict(),
    )


def summarise(cases: list[dict]) -> dict:
    by_kind: dict[str, dict] = defaultdict(
        lambda: dict(
            events=0,
            detected=0,
            target_rows=0,
            detected_rows=0,
            pre_existing_rows=0,
            incremental_detected_rows=0,
        )
    )
    controls: dict[str, dict] = defaultdict(
        lambda: dict(rows=0, error_rows=0, review_rows=0, unexpected_review_rows=0)
    )
    for case in cases:
        for item in case["injections"]:
            counter = by_kind[item["kind"]]
            counter["events"] += 1
            counter["detected"] += int(item["detected"])
            counter["target_rows"] += len(item["targets"])
            counter["detected_rows"] += len(item["detected_rows"])
            counter["pre_existing_rows"] += len(item["pre_existing_same_rule_rows"])
            counter["incremental_detected_rows"] += len(item["incremental_detected_rows"])
        for item in case["controls"]:
            counter = controls[item["kind"]]
            counter["rows"] += item["n_rows"]
            for key in ("error_rows", "review_rows", "unexpected_review_rows"):
                counter[key] += len(item[key])
    return dict(
        cases=len(cases),
        injections=dict(by_kind),
        controls=dict(controls),
        oracle_restorations=sum(case["oracle_restores_baseline_rows"] for case in cases),
        independent_metric_checks=sum(
            case[key]
            for case in cases
            for key in (
                "baseline_metrics_match_independent_oracle",
                "input_metrics_match_independent_oracle",
                "restored_metrics_match_independent_oracle",
            )
        ),
        independent_metric_check_total=len(cases) * 3,
    )


def markdown(report: dict) -> str:
    lines = [
        "# Frozen case evaluation",
        "",
        report["truth_statement"],
        "",
        "**Scope:** Six development cases and six held-out cases. All twelve were frozen before detector evaluation. "
        "Known injected edits are labels; naturally occurring baseline findings remain unlabelled. "
        "Synthetic legitimate repeated rows and premium prices may warrant review, which is counted separately from erroneous error classification.",
        "",
    ]
    for split in ("dev", "test", "all"):
        summary = report["summary"][split]
        lines += [
            f"## {split} ({summary['cases']} cases)",
            "",
            "| Injection | Detected events | Detected target rows | Already flagged before edit | New target detections |",
            "|---|---:|---:|---:|---:|",
        ]
        for kind, count in summary["injections"].items():
            lines.append(
                f"| {kind} | {count['detected']}/{count['events']} | {count['detected_rows']}/{count['target_rows']} | {count['pre_existing_rows']} | {count['incremental_detected_rows']} |"
            )
        lines += [
            "",
            "| Stipulated normal control | Rows | Error flags | Review flags | Unexpected review flags |",
            "|---|---:|---:|---:|---:|",
        ]
        for kind, count in summary["controls"].items():
            lines.append(
                f"| {kind} | {count['rows']} | {count['error_rows']} | {count['review_rows']} | {count['unexpected_review_rows']} |"
            )
        lines += [
            "",
            f"Oracle restoration: **{summary['oracle_restorations']}/{summary['cases']}** cases restored baseline included row values exactly. "
            f"Independent Decimal/ISO metric checks: **{summary['independent_metric_checks']}/{summary['independent_metric_check_total']}**.",
            "",
        ]
    lines += [
        "## Limits",
        "",
        "- These small, purposely constructed cases are an engineering regression benchmark, not an estimate of real-world precision or recall.",
        "- Original source rows are disjoint across cases and splits. Synthetic rows are labelled and are not historical UCI records.",
        "- Duplicate rows and price spikes require review; the detector is not expected to infer business intent from values alone.",
        "- Oracle repairs use hidden injection answers to validate repair mechanics; this does not measure autonomous repair accuracy.",
        "- Source workbook, case hashes, row provenance and cell changes are recorded in the case manifests and corpus.lock.json.",
        "",
    ]
    return "\n".join(lines)


def run(cases_dir: Path, output: Path) -> dict:
    lock = json.loads((cases_dir / "corpus.lock.json").read_text(encoding="utf-8"))
    for name, expected in lock["files"].items():
        if hashlib.sha256((cases_dir / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Frozen corpus mismatch: {name}")
    cases = [evaluate_case(path.parent) for path in sorted(cases_dir.glob("*/manifest.json"))]
    report = dict(
        schema_version=1,
        corpus_lock_sha256=hashlib.sha256((cases_dir / "corpus.lock.json").read_bytes()).hexdigest(),
        truth_statement="Baseline is a reference, not ground truth. Scores cover only explicit injections and stipulated synthetic controls.",
        summary={
            split: summarise([case for case in cases if split == "all" or case["split"] == split])
            for split in ("dev", "test", "all")
        },
        cases=cases,
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "evaluation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "evaluation.md").write_text(markdown(report), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "data" / "cases")
    parser.add_argument("--out", type=Path, default=ROOT / "reports")
    args = parser.parse_args()
    result = run(args.cases, args.out)
    print(json.dumps(result["summary"], indent=2))
