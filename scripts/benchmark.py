"""Measure real CSV parsing, full checks and a complete repair preview on 50k rows."""

from __future__ import annotations

import argparse
import csv
import io
import json
import platform
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from data_detective.engine import analyze, preview_repair, read_csv
from data_detective.models import ColumnMap, DatasetVersion, ParseSettings, RepairOperation, RepairPlan


def run(repeats=3):
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["order", "product", "quantity", "price", "date"])
    for i in range(50_000):
        writer.writerow([f"O{i}", f"P{i % 100}", -1 if i % 97 == 0 else 2, "12.345", f"2024-{i % 12 + 1:02}-15"])
    content = out.getvalue().encode()
    measurements = []
    for _ in range(repeats):
        start = time.perf_counter()
        columns, rows = read_csv(content)
        read_time = time.perf_counter() - start
        version = DatasetVersion("benchmark", "benchmark", None, columns, rows,
                                 ColumnMap("order", "product", "quantity", "price", "date"),
                                 ParseSettings("%Y-%m-%d"))
        start = time.perf_counter()
        result = analyze(version)
        check_time = time.perf_counter() - start
        start = time.perf_counter()
        preview = preview_repair(version, RepairPlan("benchmark", [
            RepairOperation("exclude_rows", [rows[-1].row_id]),
        ], "Benchmark exclusion"))
        preview_time = time.perf_counter() - start
        assert len(preview.version.rows) == 50_000 and result.metrics.amount_rows == 50_000
        measurements.append({"read_seconds": read_time, "analysis_seconds": check_time,
                             "read_plus_analysis_seconds": read_time + check_time,
                             "preview_seconds": preview_time})
    return {"measured_at": datetime.now(UTC).isoformat(), "platform": platform.platform(),
            "python": platform.python_version(), "rows": 50_000, "input_bytes": len(content),
            "data": "Synthetic capacity fixture, not a real-user workload", "repeats": repeats,
            "measurements": measurements,
            "median": {key: statistics.median(x[key] for x in measurements) for key in measurements[0]},
            "under_five_seconds_all_runs": all(x["read_plus_analysis_seconds"] < 5 for x in measurements),
            "scope": "Parsing and core checks only. Excludes UI render, model generation and disk persistence."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("reports/performance.json"))
    args = parser.parse_args()
    report = run()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
