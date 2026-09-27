"""Replay declared service-level tasks, preserving failures and real export files."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_detective.engine import analyze  # noqa: E402
from data_detective.exports import audit_csv, data_csv, investigation_report  # noqa: E402
from data_detective.models import (  # noqa: E402
    ColumnMap,
    ConflictError,
    ParseSettings,
    RepairOperation,
    RepairPlan,
)
from data_detective.store import Store  # noqa: E402


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rows(data: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def run_tasks(store: Store, output: Path, receipt: dict) -> None:
    def check(name: str, passed: bool, detail: object = None) -> None:
        receipt["checkpoints"].append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed:
            raise ValueError(f"Checkpoint failed: {name}")

    def load(case: str):
        path = ROOT / "data/cases" / case
        source = (path / "input.csv").read_bytes()
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        check(f"{case}: frozen source hash", sha(source) == manifest["input_sha256"])
        receipt["sources"][case] = {
            "input_sha256": sha(source), "manifest_sha256": sha((path / "manifest.json").read_bytes()),
        }
        return store.create_dataset(case, source, ColumnMap(**manifest["mapping"]),
                                    ParseSettings(**manifest["settings"])), source

    original, source = load("dev-duplicate-dispatch")
    dataset = original.dataset_id
    source_rows = rows(source)
    check("duplicate: original total", analyze(original).metrics.net_amount == "4701.37")
    selected = [original.rows[index - 1].row_id for index in (137, 138, 139)]
    reason = "Case correction note confirms only source rows 137-139 are accidental copies."
    plan = RepairPlan(original.version_id, [RepairOperation("exclude_rows", selected)], reason)
    preview = store.preview(dataset, plan)
    check("duplicate: preview has exact amount impact",
          preview.after.metrics.net_amount == "4593.97" and preview.amount_delta == "-107.40")
    check("duplicate: preview does not save", len(store.history(dataset)) == 1
          and store.active_version(dataset).version_id == original.version_id)
    check("duplicate: legitimate repeats preserved", all(not preview.version.rows[i - 1].excluded
                                                        for i in (134, 135)))
    saved = store.apply(dataset, plan, preview.fingerprint, "reviewed-repair")
    repeated = store.apply(dataset, plan, preview.fingerprint, "reviewed-repair")
    check("duplicate: repeated request is idempotent", saved.version_id == repeated.version_id
          and len(store.history(dataset)) == 2)
    rejected = False
    try:
        store.apply(dataset, plan, preview.fingerprint, "stale-new-action")
    except ConflictError:
        rejected = True
    check("duplicate: stale independent save rejected", rejected and len(store.history(dataset)) == 2)
    reopened = Store(store.root)
    persisted = reopened.active_version(dataset)
    check("duplicate: saved version survives reopening", persisted == saved)
    exported = data_csv(persisted)
    expected = [row for index, row in enumerate(source_rows, 1) if index not in (137, 138, 139)]
    check("duplicate: exported rows and every cell match independent selection", rows(exported) == expected
          and len(expected) == 136)
    audit = audit_csv(reopened, dataset, saved.version_id)
    edits = [row for row in rows(audit) if row["kind"] == "repair"]
    check("duplicate: complete exclusion audit", len(edits) == 3
          and {row["row_id"] for row in edits} == set(selected)
          and all(row["reason"] == reason for row in edits))
    report = investigation_report(reopened, dataset, saved.version_id)
    check("duplicate: report binds original source and saved version",
          sha(source) in report and saved.version_id in report and "4593.97" in report)
    artifacts = {"repaired.csv": exported, "repair-audit.csv": audit,
                 "repair-report.md": report.encode("utf-8")}
    restored = reopened.restore(dataset, original.version_id, saved.version_id,
                                "Restore original contents; retain reviewed repair in history.", "restore-original")
    restored_audit = audit_csv(reopened, dataset, restored.version_id)
    check("duplicate: restore preserves original rows and total", restored.rows == original.rows
          and analyze(restored).metrics.net_amount == "4701.37")
    check("duplicate: restore retains all history and target", len(reopened.history(dataset)) == 3
          and any(row["kind"] == "restore" and row["restored_from"] == original.version_id
                  for row in rows(restored_audit)))
    check("duplicate: immutable original download", reopened.original_bytes(dataset) == source)
    artifacts["restored-audit.csv"] = restored_audit
    artifacts["restored.csv"] = data_csv(restored)
    date_original, date_source = load("dev-broken-dates")
    date_plan = RepairPlan(date_original.version_id, [RepairOperation(
        "set_cell", [date_original.rows[i - 1].row_id for i in (27, 31)],
        "InvoiceDate", "2011-03-15 13:23:00")], "Use the case correction note for rows 27 and 31.")
    date_preview = store.preview(date_original.dataset_id, date_plan)
    before, after = date_preview.before.metrics, date_preview.after.metrics
    check("date: exact unassigned-to-month impact",
          before.net_amount == after.net_amount == "3203.66" and date_preview.amount_delta == "0.00"
          and before.unassigned_amount == "37.20" and after.unassigned_amount == "0.00"
          and date_preview.monthly_delta.get("2011-03") == "37.20")
    check("date: only documented cells changed", len(date_preview.changes) == 2
          and {change.row_id for change in date_preview.changes} == set(date_plan.operations[0].row_ids)
          and all(change.column == "InvoiceDate" for change in date_preview.changes))
    check("date: abandoned preview leaves persistent state untouched",
          store.active_version(date_original.dataset_id) == date_original
          and len(store.history(date_original.dataset_id)) == 1
          and store.original_bytes(date_original.dataset_id) == date_source)
    for filename, payload in artifacts.items():
        (output / filename).write_bytes(payload)
        receipt["exports"][filename] = {"sha256": sha(payload), "bytes": len(payload)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/independent-task-check")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output directory to preserve earlier receipts")
    args.output.mkdir(parents=True)
    receipt = {
        "schema_version": 1, "started_at_utc": datetime.now(UTC).isoformat(),
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "runner_sha256": sha(Path(__file__).read_bytes()),
        "measurement": "assistant service-level functional dry-run on known development fixtures",
        "participants": 0, "model_calls": 0, "browser_test": False,
        "protocol_sha256": sha((ROOT / "docs/INDEPENDENT_TASK_CHECK.md").read_bytes()),
        "sources": {}, "checkpoints": [], "exports": {}, "status": "running",
    }
    try:
        with tempfile.TemporaryDirectory(prefix="detective-task-check-") as directory:
            run_tasks(Store(directory), args.output, receipt)
        receipt["status"] = "passed"
    except Exception as error:
        receipt["status"] = "failed"
        receipt["error"] = f"{type(error).__name__}: {error}"
    receipt["finished_at_utc"] = datetime.now(UTC).isoformat()
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"{receipt['status']}: {len(receipt['checkpoints'])} checkpoints; {args.output}")
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
