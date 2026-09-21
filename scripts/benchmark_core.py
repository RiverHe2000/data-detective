"""Reproduce the serializer optimization comparison with the same current engine.

Run from the project root:
  .venv-clean/Scripts/python.exe scripts/benchmark_core.py --out reports/core-optimization.json

The baseline restores only DatasetVersion.to_dict = dataclasses.asdict in memory.
It is a controlled comparison of one change, not a benchmark of an old release.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import inspect
import json
import os
import platform
import statistics
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from data_detective.engine import preview_repair
from data_detective.models import (
    ColumnMap,
    DataRow,
    DatasetVersion,
    ParseSettings,
    Preview,
    RepairOperation,
    RepairPlan,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATHS = (
    "scripts/benchmark_core.py",
    "src/data_detective/models.py",
    "src/data_detective/engine.py",
)
LOCK_PATHS = ("requirements-windows.lock", "requirements-dev-windows.lock")
ROWS = 50_000
REPEATS = 3


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hashes(paths: tuple[str, ...]) -> dict[str, str]:
    return {path: sha256((ROOT / path).read_bytes()) for path in paths}


def legacy_to_dict(version: DatasetVersion) -> dict[str, Any]:
    """Exact pre-optimization DatasetVersion.to_dict body, reconstructed locally."""
    return asdict(version)


def fixture() -> tuple[DatasetVersion, RepairPlan]:
    columns = ["order", "product", "qty", "price", "date"]
    rows = [
        DataRow(str(index), dict(zip(columns, [
            str(index), str(index % 500), "1", "1.2345", "2024-01-01",
        ], strict=True)))
        for index in range(ROWS)
    ]
    version = DatasetVersion(
        "v", "d", None, columns, rows, ColumnMap(*columns), ParseSettings("%Y-%m-%d"),
    )
    plan = RepairPlan("v", [RepairOperation("set_cell", ["0"], "qty", "2")], "verified source")
    return version, plan


def preview_semantics(result: Preview, serializer: Any) -> dict[str, Any]:
    """Include all deterministic output; omit new UUID/time and elapsed timings."""
    snapshot = serializer(result.version)
    snapshot.pop("version_id")
    snapshot.pop("created_at")
    return {
        "plan": result.plan.to_dict(),
        "snapshot": snapshot,
        "before": {
            "metrics": result.before.metrics.to_dict(),
            "findings": [item.to_dict() for item in result.before.findings],
        },
        "after": {
            "metrics": result.after.metrics.to_dict(),
            "findings": [item.to_dict() for item in result.after.findings],
        },
        "changes": [asdict(item) for item in result.changes],
        "amount_delta": result.amount_delta,
        "monthly_delta": result.monthly_delta,
        "fingerprint": result.fingerprint,
    }


def environment() -> dict[str, Any]:
    packages = {}
    for name in ("data-detective", "pandas", "streamlit", "plotly", "numpy"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    config = Path(sys.prefix) / "pyvenv.cfg"
    return {
        "executable": sys.executable,
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "venv_config": config.read_text(encoding="utf-8") if config.exists() else None,
        "packages": packages,
        "gc_enabled": gc.isenabled(),
        "gc_thresholds": list(gc.get_threshold()),
        "perf_counter": vars(time.get_clock_info("perf_counter")),
    }


def run() -> dict[str, Any]:
    started = datetime.now(UTC).isoformat()
    sources_before = file_hashes(SOURCE_PATHS)
    current_serializer = DatasetVersion.to_dict
    serializers = {"legacy_asdict": legacy_to_dict, "current_detached_containers": current_serializer}
    version, plan = fixture()
    input_bytes = canonical(current_serializer(version))
    expected_input_hash = sha256(input_bytes)
    if canonical(legacy_to_dict(version)) != input_bytes:
        raise AssertionError("Baseline and current serializers have different input content.")
    reference = preview_repair(version, plan)
    expected_preview_hash = sha256(canonical(preview_semantics(reference, current_serializer)))
    expected_metrics = {
        "before": reference.before.metrics.to_dict(),
        "after": reference.after.metrics.to_dict(),
        "amount_delta": reference.amount_delta,
        "monthly_delta": reference.monthly_delta,
        "fingerprint": reference.fingerprint,
    }
    del reference
    measurements = []
    verified_outputs = 0

    for scope in ("snapshot_to_dict", "complete_preview"):
        # Each variant receives one explicit untimed warm-up for this scope.
        for serializer in serializers.values():
            with patch.object(DatasetVersion, "to_dict", serializer):
                output = version.to_dict() if scope == "snapshot_to_dict" else preview_repair(version, plan)
            del output
        for repeat in range(1, REPEATS + 1):
            order = list(serializers) if repeat % 2 else list(reversed(serializers))
            for position, name in enumerate(order, start=1):
                gc.collect()  # Outside the timer; normal automatic GC remains enabled.
                with patch.object(DatasetVersion, "to_dict", serializers[name]):
                    if scope == "snapshot_to_dict":
                        began = time.perf_counter_ns()
                        output = version.to_dict()
                        elapsed_ns = time.perf_counter_ns() - began
                    else:
                        began = time.perf_counter_ns()
                        output = preview_repair(version, plan)
                        elapsed_ns = time.perf_counter_ns() - began
                # Correctness and result disposal are deliberately outside the timer.
                if scope == "snapshot_to_dict":
                    output_hash = sha256(canonical(output))
                    expected_hash = expected_input_hash
                else:
                    output_hash = sha256(canonical(preview_semantics(output, current_serializer)))
                    expected_hash = expected_preview_hash
                if output_hash != expected_hash:
                    raise AssertionError(f"Semantic output changed: {scope}, {name}, repeat {repeat}.")
                verified_outputs += 1
                measurements.append({
                    "scope": scope, "variant": name, "repeat": repeat, "position_in_pair": position,
                    "elapsed_ns": elapsed_ns, "elapsed_seconds": elapsed_ns / 1_000_000_000,
                    "semantic_sha256": output_hash,
                })
                del output

    summaries = {}
    for scope in ("snapshot_to_dict", "complete_preview"):
        medians = {
            name: statistics.median(
                item["elapsed_seconds"] for item in measurements
                if item["scope"] == scope and item["variant"] == name
            ) for name in serializers
        }
        baseline, current = medians.values()
        summaries[scope] = {
            "median_seconds": medians,
            "baseline_over_current_ratio": baseline / current,
            "median_time_reduction_percent": (1 - current / baseline) * 100,
        }
    sources_after = file_hashes(SOURCE_PATHS)
    if sources_after != sources_before:
        raise RuntimeError("Benchmark source changed during execution; rerun after edits finish.")
    if sha256(canonical(current_serializer(version))) != expected_input_hash:
        raise AssertionError("The input fixture was mutated during the benchmark.")
    return {
        "schema_version": 1,
        "started_at": started,
        "completed_at": datetime.now(UTC).isoformat(),
        "command": ".venv-clean/Scripts/python.exe scripts/benchmark_core.py --out reports/core-optimization.json",
        "comparison": {
            "type": "Controlled serializer substitution with the current engine in both variants",
            "baseline_reconstruction": inspect.getsource(legacy_to_dict),
            "current_implementation": inspect.getsource(current_serializer),
            "patch": "unittest.mock.patch.object(DatasetVersion, 'to_dict', serializer); no source edits",
            "limits": [
                "This restores only the former serializer, not an entire historical application revision.",
                "Single-machine warm synthetic observation; three repeats are not a statistical guarantee.",
                "No memory, UI, disk persistence, CSV parsing, GPU or model performance claim is made.",
                "Fresh traceable results supersede earlier unrecorded conversational measurements.",
            ],
        },
        "input": {
            "construction": inspect.getsource(fixture),
            "rows": ROWS, "columns": version.columns, "distinct_products": 500,
            "distinct_dates": 1, "excluded_rows": 0,
            "canonical_snapshot_bytes": len(input_bytes), "canonical_snapshot_sha256": expected_input_hash,
            "canonicalization": "UTF-8 JSON, ensure_ascii=False, sort_keys=True, separators=(',', ':')",
            "plan": plan.to_dict(), "plan_sha256": sha256(canonical(plan.to_dict())),
            "expected_metrics": expected_metrics,
        },
        "measurement_policy": {
            "repeats_per_scope_per_variant": REPEATS,
            "warmups_per_scope_per_variant": 1,
            "additional_reference_preview": "One untimed current-engine preview before warm-ups for semantic checks",
            "variant_order": "Baseline/current in repeats 1 and 3; current/baseline in repeat 2",
            "clock": "time.perf_counter_ns",
            "gc": "gc.collect before each measured call, outside the timer; normal automatic GC retained",
            "snapshot_to_dict": "One complete DatasetVersion.to_dict allocation, no JSON encoding",
            "complete_preview": (
                "One complete preview_repair: validation, copying, before/after analysis, Decimal deltas, "
                "new UUID/time and canonical source/plan fingerprint"
            ),
            "excluded": [
                "Input construction", "CSV decoding", "patch setup/teardown", "explicit gc.collect",
                "correctness checks", "result disposal", "disk/SQLite", "UI", "model", "report writing",
            ],
        },
        "environment": environment(),
        "source_sha256_before": sources_before,
        "source_sha256_after": sources_after,
        "source_unchanged_during_run": True,
        "dependency_lock_sha256": file_hashes(LOCK_PATHS),
        "correctness": {
            "serializer_outputs_equal": True, "measured_outputs_verified": verified_outputs,
            "preview_semantic_sha256": expected_preview_hash,
            "preview_comparison_excludes": ["random new version IDs", "created_at", "analysis elapsed_seconds"],
            "input_unchanged": True,
        },
        "measurements": measurements,
        "summary": summaries,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "reports/core-optimization.json")
    args = parser.parse_args()
    report = run()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.out.resolve()), "summary": report["summary"]}, indent=2))
