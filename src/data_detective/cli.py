"""Small CLI for script-first learning and repeatable local checks."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .engine import analyze
from .exports import audit_csv, data_csv, investigation_report
from .models import ColumnMap, ParseSettings, RepairPlan
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="workspace")
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="Import a CSV with explicit mapping and parse settings")
    imp.add_argument("csv", type=Path)
    imp.add_argument("--name", required=True)
    imp.add_argument("--mapping", type=Path, help="JSON object containing the five ColumnMap fields")
    imp.add_argument("--date-format", default="%Y-%m-%d %H:%M:%S")
    imp.add_argument("--currency", default="GBP")
    imp.add_argument("--decimal", default=".")
    imp.add_argument("--thousands", default="")
    imp.add_argument("--delimiter", default=",")
    sub.add_parser("list")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("dataset")
    preview = sub.add_parser("preview")
    preview.add_argument("dataset")
    preview.add_argument("plan", type=Path)
    apply = sub.add_parser("apply")
    apply.add_argument("dataset")
    apply.add_argument("plan", type=Path)
    apply.add_argument("--fingerprint", required=True)
    apply.add_argument("--key", required=True)
    export = sub.add_parser("export")
    export.add_argument("dataset")
    export.add_argument("--out", type=Path, default=Path("output"))
    args = parser.parse_args()
    store = Store(args.workspace)
    if args.command == "import":
        mapping = ColumnMap(**json.loads(args.mapping.read_text("utf-8"))) if args.mapping else ColumnMap(
            "InvoiceNo", "StockCode", "Quantity", "UnitPrice", "InvoiceDate")
        version = store.create_dataset(args.name, args.csv.read_bytes(), mapping,
                                       ParseSettings(args.date_format, args.currency, args.decimal, args.thousands),
                                       args.delimiter)
        result = {"dataset_id": version.dataset_id, "version_id": version.version_id}
    elif args.command == "list":
        result = store.list_datasets()
    elif args.command == "inspect":
        result = asdict(analyze(store.active_version(args.dataset)))
    elif args.command in {"preview", "apply"}:
        plan = RepairPlan.from_dict(json.loads(args.plan.read_text("utf-8")))
        if args.command == "apply":
            version = store.apply(args.dataset, plan, args.fingerprint, args.key)
            result = {"version_id": version.version_id}
        else:
            preview = store.preview(args.dataset, plan)
            result = {"fingerprint": preview.fingerprint, "before": asdict(preview.before.metrics),
                      "after": asdict(preview.after.metrics), "changes": [asdict(c) for c in preview.changes]}
    else:
        args.out.mkdir(parents=True, exist_ok=True)
        version = store.active_version(args.dataset)
        (args.out / "data.csv").write_bytes(data_csv(version))
        (args.out / "changes.csv").write_bytes(audit_csv(store, args.dataset, version.version_id))
        (args.out / "report.md").write_text(investigation_report(store, args.dataset, version.version_id), encoding="utf-8")
        result = {"exported_to": str(args.out.resolve())}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
