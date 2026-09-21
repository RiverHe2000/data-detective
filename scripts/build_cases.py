"""Build twelve frozen, source-disjoint cases; annotations never use the detector."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
BUILDER_VERSION = "1.0.0"
KINDS = (
    "duplicate-dispatch",
    "missing-fields",
    "broken-numbers",
    "broken-dates",
    "price-spike",
    "mixed-incident",
)
MAPPING = dict(
    order_id="InvoiceNo",
    product_id="StockCode",
    quantity="Quantity",
    unit_price="UnitPrice",
    order_time="InvoiceDate",
)
SETTINGS = dict(
    date_format="%Y-%m-%d %H:%M:%S", currency="GBP", decimal_separator=".", thousands_separator=""
)
DISCLAIMER = "Baseline is an unclean real-data reference, not ground truth. Only deliberate edits and explicitly synthetic controls have known labels."


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def csv_bytes(columns: list[str], rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def serialise(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, float):
        return format(Decimal(str(value)), "f").removesuffix(".0")
    return str(value)


def source_windows(workbook: Path) -> tuple[list[str], list[list[dict[str, str]]]]:
    book = openpyxl.load_workbook(workbook, read_only=True, data_only=True)
    try:
        iterator = book.active.iter_rows(values_only=True)
        columns = [str(value) for value in next(iterator)]
        groups: list[list[dict[str, str]]] = [[] for _ in range(12)]
        for index, values in enumerate(iterator):
            group = index // 40_000
            if group >= 12:
                break
            if index % 40_000 < 120:
                groups[group].append(dict(zip(columns, map(serialise, values), strict=True)))
            if index >= 440_119:
                break
        if any(len(rows) != 120 for rows in groups):
            raise ValueError("The pinned corpus no longer has the expected source windows")
        return columns, groups
    finally:
        book.close()


def make_case(
    index: int, columns: list[str], original: list[dict[str, str]], receipt: dict
) -> dict[str, bytes]:
    split = "dev" if index < 6 else "test"
    kind = KINDS[index % 6]
    slug = f"{split}-{kind}"
    baseline = [dict(row) for row in original]
    provenance = [
        dict(
            input_row_index=i + 1,
            origin="uci",
            source_data_row=index * 40_000 + i + 1,
            source_excel_row=index * 40_000 + i + 2,
        )
        for i in range(len(baseline))
    ]
    target = next(
        i
        for i, row in enumerate(baseline)
        if row["UnitPrice"] and Decimal(row["UnitPrice"]) > 0 and row["Quantity"]
    )
    template = dict(baseline[target])
    controls: list[dict] = []

    def synthetic(label: str, overrides: dict[str, str]) -> int:
        row = dict(template)
        row.update(overrides)
        baseline.append(row)
        number = len(baseline)
        provenance.append(
            dict(
                input_row_index=number,
                origin="synthetic_control",
                label=label,
                based_on_source_data_row=index * 40_000 + target + 1,
            )
        )
        return number

    # Fixed support makes the review case testable; these are clearly not historical observations.
    support = []
    for n in range(10):
        support.append(
            synthetic(
                "synthetic price reference",
                {
                    "InvoiceNo": f"DD-{index}-REF-{n}",
                    "Quantity": "1",
                    "InvoiceDate": f"2011-01-{n + 1:02d} 12:00:00",
                },
            )
        )
    controls.append(dict(kind="price_reference", input_row_indices=support, allowed_review_rules=[]))
    cancellation = synthetic(
        "stipulated legitimate cancellation",
        {
            "InvoiceNo": f"CDD-{index}-RETURN",
            "StockCode": f"DD-{index}-RETURN",
            "Quantity": "-2",
            "UnitPrice": "2.50",
        },
    )
    controls.append(
        dict(kind="legitimate_cancellation", input_row_indices=[cancellation], allowed_review_rules=[])
    )
    multi = [
        synthetic(
            "stipulated multi-product order",
            {
                "InvoiceNo": f"DD-{index}-MULTI",
                "StockCode": f"DD-{index}-MULTI-{n}",
                "Quantity": "1",
                "UnitPrice": "3.25",
            },
        )
        for n in range(2)
    ]
    controls.append(dict(kind="multi_product_order", input_row_indices=multi, allowed_review_rules=[]))
    repeat = [
        synthetic(
            "stipulated two legitimate identical sale events",
            {
                "InvoiceNo": f"DD-{index}-REPEAT",
                "StockCode": f"DD-{index}-REPEAT",
                "Quantity": "1",
                "UnitPrice": "5",
            },
        )
        for _ in range(2)
    ]
    controls.append(
        dict(kind="legitimate_repeat", input_row_indices=repeat, allowed_review_rules=["duplicate_rows"])
    )
    high = synthetic(
        "stipulated legitimate premium product",
        {
            "InvoiceNo": f"DD-{index}-PREMIUM",
            "StockCode": f"DD-{index}-PREMIUM",
            "Quantity": "1",
            "UnitPrice": "999.99",
        },
    )
    controls.append(
        dict(
            kind="legitimate_premium_price", input_row_indices=[high], allowed_review_rules=["price_outlier"]
        )
    )
    current = [dict(row) for row in baseline]
    injections: list[dict] = []
    repairs: list[dict] = []

    def mutate(
        row_index: int, column: str, value: str, injection_kind: str, rule: str, severity: str
    ) -> None:
        previous = current[row_index - 1][column]
        current[row_index - 1][column] = value
        injections.append(
            dict(
                kind=injection_kind,
                rule_id=rule,
                expected_severity=severity,
                input_row_indices=[row_index],
                cell_diff=[
                    dict(
                        input_row_index=row_index,
                        source_data_row=provenance[row_index - 1].get("source_data_row"),
                        column=column,
                        before=previous,
                        after=value,
                    )
                ],
            )
        )
        repairs.append(dict(kind="set_cell", row_indices=[row_index], column=column, value=previous))

    def duplicate() -> None:
        added = []
        diffs = []
        for original_index in (22, 35, 48):
            current.append(dict(current[original_index - 1]))
            number = len(current)
            added.append(number)
            provenance.append(
                dict(
                    input_row_index=number,
                    origin="injected_copy",
                    copied_input_row_index=original_index,
                    source_data_row=index * 40_000 + original_index,
                )
            )
            diffs.append(
                dict(
                    input_row_index=number,
                    copied_input_row_index=original_index,
                    source_data_row=index * 40_000 + original_index,
                    before=None,
                    after=dict(current[-1]),
                )
            )
        injections.append(
            dict(
                kind="copied_records",
                rule_id="duplicate_rows",
                expected_severity="review",
                input_row_indices=added,
                cell_diff=diffs,
            )
        )
        repairs.append(dict(kind="exclude_rows", row_indices=added))

    if kind in {"duplicate-dispatch", "mixed-incident"}:
        duplicate()
    if kind in {"missing-fields", "mixed-incident"}:
        mutate(6, "UnitPrice", "", "missing_value", "missing_required", "error")
        mutate(12, "InvoiceNo", "", "missing_value", "missing_required", "error")
    if kind in {"broken-numbers", "mixed-incident"}:
        mutate(17, "Quantity", "two", "numeric_corruption", "numeric_parse", "error")
        mutate(19, "UnitPrice", "1.2.3", "numeric_corruption", "numeric_parse", "error")
    if kind in {"broken-dates", "mixed-incident"}:
        mutate(27, "InvoiceDate", "2011-02-30 09:15:00", "date_corruption", "date_parse", "error")
        mutate(31, "InvoiceDate", "not-a-date", "date_corruption", "date_parse", "error")
    if kind in {"price-spike", "mixed-incident"}:
        mutate(
            target + 1,
            "UnitPrice",
            format(Decimal(template["UnitPrice"]) * 1000, "f"),
            "price_spike",
            "price_outlier",
            "review",
        )
    before_bytes = csv_bytes(columns, baseline)
    after_bytes = csv_bytes(columns, current)
    manifest = dict(
        schema_version=1,
        builder_version=BUILDER_VERSION,
        case_id=slug,
        split=split,
        title=kind.replace("-", " ").title(),
        showcase=index in (0, 3, 4),
        truth_statement=DISCLAIMER,
        columns=columns,
        mapping=MAPPING,
        settings=SETTINGS,
        source={
            key: receipt[key]
            for key in (
                "source_url",
                "source_page",
                "attribution",
                "license",
                "license_url",
                "archive_sha256",
                "workbook_sha256",
            )
        },
        source_window=dict(
            first_data_row=index * 40_000 + 1,
            last_data_row=index * 40_000 + 120,
            count=120,
            numbering="1-based data rows; Excel worksheet row = data row + 1",
        ),
        transformations=[
            "InvoiceDate serialized to ISO 8601 without timezone",
            "Excel blanks represented as empty CSV fields",
            "16 explicitly synthetic controls appended before injection",
        ],
        baseline_sha256=digest(before_bytes),
        input_sha256=digest(after_bytes),
        baseline_rows=len(baseline),
        input_rows=len(current),
        row_provenance=provenance,
        injections=injections,
        controls=controls,
        oracle_repairs=repairs,
        answer=dict(
            known_injection_events=len(injections),
            restoration_target="included row values exactly equal baseline in order",
            price_spike_policy="review only: ground truth records the intentional edit, not that any high price must be wrong",
        ),
    )
    return {
        f"{slug}/baseline.csv": before_bytes,
        f"{slug}/input.csv": after_bytes,
        f"{slug}/manifest.json": (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
    }


def build(private: Path, destination: Path) -> dict:
    receipt = json.loads((private / "source_receipt.json").read_text(encoding="utf-8"))
    workbook = private / "Online Retail.xlsx"
    if digest(workbook.read_bytes()) != receipt["workbook_sha256"]:
        raise ValueError("Workbook does not match the source receipt")
    columns, groups = source_windows(workbook)
    artifacts: dict[str, bytes] = {}
    for index, rows in enumerate(groups):
        artifacts.update(make_case(index, columns, rows, receipt))
    lock = dict(
        builder_version=BUILDER_VERSION,
        source_workbook_sha256=receipt["workbook_sha256"],
        policy="All cases frozen before detector evaluation; changed files fail regeneration.",
        files={name: digest(content) for name, content in sorted(artifacts.items())},
    )
    artifacts["corpus.lock.json"] = (json.dumps(lock, indent=2) + "\n").encode()
    # Validate the whole batch before writing. No flag silently overwrites a locked test set.
    for name, content in artifacts.items():
        path = destination / name
        if path.exists() and path.read_bytes() != content:
            raise ValueError(f"Frozen case differs: {name}. Create a separately versioned corpus instead.")
    for name, content in artifacts.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(content)
    return dict(cases=12, dev=6, test=6, source_rows=1440, files=len(artifacts), destination=str(destination))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private", type=Path, default=ROOT / "data" / "private")
    parser.add_argument("--destination", type=Path, default=ROOT / "data" / "cases")
    args = parser.parse_args()
    print(json.dumps(build(args.private, args.destination), indent=2))
