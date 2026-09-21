from __future__ import annotations

import csv
import io
from dataclasses import replace
from decimal import Decimal

import pytest

from data_detective import engine
from data_detective.engine import analyze, preview_repair, read_csv, validate_contract
from data_detective.models import (
    MAX_BYTES,
    ColumnMap,
    ConflictError,
    DatasetVersion,
    ParseSettings,
    RepairOperation,
    RepairPlan,
    ValidationError,
)

COLUMNS = ["Order", "Product", "Quantity", "Price", "Date", "Description"]
MAPPING = ColumnMap("Order", "Product", "Quantity", "Price", "Date")
SETTINGS = ParseSettings(date_format="%Y-%m-%d")


def version(records=None, *, settings=SETTINGS):
    if records is None:
        records = [["O1", "A", "2", "3.50", "2024-01-01", "normal"]]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(COLUMNS)
    writer.writerows(records)
    columns, rows = read_csv(buffer.getvalue().encode("utf-8"))
    return DatasetVersion("v1", "dataset", None, columns, rows, MAPPING, settings)


def repair(source, operations, reason="Checked against source"):
    return RepairPlan(source.version_id, operations, reason)


def rules(result):
    return {finding.rule_id for finding in result.findings}


def test_csv_preserves_strings_bom_quoted_newlines_and_stable_identity():
    source = b'\xef\xbb\xbfOrder,Product\r\n001,"a\nb"\r\n'
    columns, rows = read_csv(source)
    assert columns == ["Order", "Product"]
    assert rows[0].values == {"Order": "001", "Product": "a\nb"}
    assert read_csv(source)[1][0].row_id == rows[0].row_id
    assert read_csv(source + b"002,c\r\n")[1][0].row_id != rows[0].row_id


@pytest.mark.parametrize("data", [
    b"", b"a,a\n1,2", b"a, a \n1,2", b"a, \n1,2", b"a,b\n1", b"a,b\n1,2,3",
    b'a,b\n1,"unclosed', b'a,b\n1,"closed"bad', b'a,b\n1,stray"quote', b"a,b\n\n", b"a,b\n\xff,2", b"a,b\n1,\x002",
])
def test_malformed_csv_is_rejected(data):
    with pytest.raises(ValidationError):
        read_csv(data)


def test_csv_upload_and_row_limits(monkeypatch):
    with pytest.raises(ValidationError, match="20 MiB"):
        read_csv(b"x" * (MAX_BYTES + 1))
    monkeypatch.setattr(engine, "MAX_ROWS", 2)
    assert len(read_csv(b"a\n1\n2\n")[1]) == 2
    with pytest.raises(ValidationError, match="50,000"):
        read_csv(b"a\n1\n2\n3\n")


def test_csv_escaped_quotes_and_custom_delimiter_are_valid():
    columns, rows = read_csv(b'a;b\n1;"escaped ""quote"""\n', delimiter=";")
    assert columns == ["a", "b"]
    assert rows[0].values["b"] == 'escaped "quote"'


def test_mapping_and_explicit_settings_validation():
    validate_contract(COLUMNS, MAPPING, SETTINGS)
    with pytest.raises(ValidationError):
        validate_contract(COLUMNS, replace(MAPPING, product_id="Order"), SETTINGS)
    with pytest.raises(ValidationError):
        validate_contract(COLUMNS, replace(MAPPING, quantity="Unknown"), SETTINGS)
    for settings in [
        replace(SETTINGS, date_format="%d/%m/%y"), replace(SETTINGS, date_format="%m/%d"),
        replace(SETTINGS, date_format="%d/%b/%Y"), replace(SETTINGS, date_format="%Y-%m-%d %m"),
        replace(SETTINGS, thousands_separator="."), replace(SETTINGS, currency="gbp"),
    ]:
        with pytest.raises(ValidationError):
            validate_contract(COLUMNS, MAPPING, settings)


def test_returns_cancellations_and_multiline_orders_are_normal():
    data = version([
        ["100", "A", "2", "10", "2024-01-01", "sale"],
        ["100", "B", "1", "5", "2024-01-01", "second product"],
        ["C100", "A", "-1", "10", "2024-01-01", "return"],
        ["101", "A", "0", "10", "2024-01-01", "zero quantity"],
    ])
    result = analyze(data)
    assert result.metrics.net_amount == "15.00"
    assert result.metrics.amount_rows == 4
    assert result.findings == []


def test_decimal_amounts_remain_exact_without_currency_rounding():
    source = version([
        ["1", "A", "3", "0.1", "2024-01-01", ""],
        ["2", "A", "1", "0.000000000001", "2024-01-01", ""],
        ["3", "B", "1000000000", "1000000000000", "2024-01-01", ""],
    ])
    assert analyze(source).metrics.net_amount == "1000000000000000000000.300000000001"


@pytest.mark.parametrize("quantity,price", [
    ("1.5", "2"), ("1e2", "2"), ("1", "NaN"), ("1", "inf"), ("1", "1,25"),
    ("1000000001", "2"), ("1", "1000000000001"), ("1", "0.0000000000001"),
])
def test_invalid_numbers_are_not_zero_filled(quantity, price):
    source = version([["1", "A", quantity, price, "2024-01-01", ""]])
    result = analyze(source)
    assert rules(result) == {"numeric_parse"}
    assert result.metrics.amount_rows == 0
    assert result.metrics.invalid_amount_rows == 1


def test_thousands_and_decimal_format_is_explicit_and_strict():
    settings = replace(SETTINGS, decimal_separator=",", thousands_separator=".")
    source = version([
        ["1", "A", "1.000", "1.234,56", "2024-01-01", ""],
        ["2", "A", "1", "12.34,56", "2024-01-01", ""],
        ["3", "A", "1,0", "0,01", "2024-01-01", ""],
    ], settings=settings)
    result = analyze(source)
    assert result.metrics.net_amount == "1234560.01"
    assert result.metrics.invalid_amount_rows == 1
    assert result.findings[0].row_ids == [source.rows[1].row_id]


def test_missing_cells_have_one_finding_per_field_not_parse_duplicates():
    source = version([["", " ", "", "", "", ""]])
    result = analyze(source)
    assert rules(result) == {"missing_required"}
    assert len(result.findings) == 5
    assert result.metrics.invalid_date_rows == 1
    assert result.metrics.invalid_amount_rows == 1


def test_invalid_date_keeps_amount_and_reports_unassigned_coverage():
    source = version([
        ["1", "A", "1", "3", "2024-01-01", ""],
        ["2", "A", "-1", "2", "2024-02-30", ""],
        ["3", "A", "2", "4", "", ""],
    ])
    result = analyze(source)
    assert result.metrics.net_amount == "9.00"
    assert result.metrics.monthly == {"2024-01": "3.00"}
    assert result.metrics.unassigned_amount == "6.00"
    assert result.metrics.invalid_date_rows == 2
    assert rules(result) == {"date_parse", "missing_required"}


def test_ambiguous_date_change_moves_month_without_changing_net():
    source = version([["1", "A", "1", "10", "01/02/2024", ""]], settings=replace(SETTINGS, date_format="%d/%m/%Y"))
    plan = repair(source, [RepairOperation("change_settings", settings={"date_format": "%m/%d/%Y"})])
    preview = preview_repair(source, plan)
    assert preview.before.metrics.monthly == {"2024-02": "10.00"}
    assert preview.after.metrics.monthly == {"2024-01": "10.00"}
    assert preview.amount_delta == "0.00"
    assert preview.monthly_delta == {"2024-01": "10.00", "2024-02": "-10.00"}
    assert preview.changes[0].row_id == "__settings__"
    assert source.settings.date_format == "%d/%m/%Y"


def test_compact_dates_require_fixed_width_while_delimited_dates_allow_unpadded_fields():
    compact = version([["1", "A", "1", "10", "2024111", ""]], settings=replace(SETTINGS, date_format="%Y%m%d"))
    assert rules(analyze(compact)) == {"date_parse"}
    unpadded = version([["1", "A", "1", "10", "1/2/2024 8:26", ""]], settings=replace(SETTINGS, date_format="%d/%m/%Y %H:%M"))
    assert analyze(unpadded).metrics.monthly == {"2024-02": "10.00"}


def test_duplicate_candidates_compare_every_raw_column_and_remain_included():
    record = ["1", "A", "1", "10", "2024-01-01", "a"]
    source = version([record, record, [*record[:-1], "b"], ["1", "B", *record[2:]]])
    result = analyze(source)
    duplicate = [finding for finding in result.findings if finding.rule_id == "duplicate_rows"]
    assert len(duplicate) == 1
    assert duplicate[0].row_ids == [row.row_id for row in source.rows[:2]]
    assert duplicate[0].severity == "review"
    assert result.metrics.net_amount == "40.00"
    assert result.metrics.excluded_rows == 0


def test_price_outlier_minimum_sample_zero_iqr_and_review_only():
    records = [[str(i), "A", "1", "5", "2024-01-01", ""] for i in range(9)]
    source = version([*records, ["last", "A", "1", "500", "2024-01-01", ""]])
    result = analyze(source)
    finding = next(f for f in result.findings if f.rule_id == "price_outlier")
    assert finding.severity == "review"
    assert finding.row_ids == [source.rows[-1].row_id]
    assert finding.evidence["iqr"] == "0.00"
    assert result.metrics.net_amount == "545.00"
    assert "price_outlier" not in rules(analyze(version(records[:-1] + [records[-1][:-3] + ["500", "2024-01-01", ""]])))


def test_price_outliers_use_three_iqr_instead_of_a_fixed_price_threshold():
    records = [[str(i), "A", "1", str(price), "2024-01-01", ""] for i, price in enumerate([1, 2, 3, 4, 5, 6, 7, 8, 9, 100])]
    source = version(records)
    finding = next(f for f in analyze(source).findings if f.rule_id == "price_outlier")
    assert finding.evidence["q1"] == "3.25"
    assert finding.evidence["q3"] == "7.75"
    assert finding.evidence["upper_bound"] == "21.25"
    assert finding.row_ids == [source.rows[-1].row_id]


def test_multistep_preview_is_pure_preserves_rows_and_records_each_change():
    source = version([
        ["1", " A ", "bad", "2", "2024-01-01", "normal  spacing"],
        ["2", "B", "1", "3", "2024-01-01", "retain source"],
    ])
    original = source.to_dict()
    first, second = [row.row_id for row in source.rows]
    plan = repair(source, [
        RepairOperation("set_cell", [first], "Quantity", "2"),
        RepairOperation("set_cell", [first], "Quantity", "3"),
        RepairOperation("normalize_whitespace", [first], "Product"),
        RepairOperation("exclude_rows", [second]),
    ])
    preview = preview_repair(source, plan)
    assert source.to_dict() == original
    assert preview.version.parent_version_id == source.version_id
    assert [row.row_id for row in preview.version.rows] == [first, second]
    assert preview.version.rows[1].values == source.rows[1].values
    assert preview.version.rows[1].excluded is True
    assert preview.version.rows[0].values["Description"] == "normal  spacing"
    assert preview.after.metrics.net_amount == "6.00"
    assert preview.amount_delta == "3.00"
    assert [(c.before, c.after) for c in preview.changes[:2]] == [("bad", "2"), ("2", "3")]
    assert preview.changes[-1].column == "__excluded__"
    again = preview_repair(source, plan)
    assert again.version.version_id != preview.version.version_id
    assert again.fingerprint == preview.fingerprint


def test_preview_fingerprint_binds_full_parent_content_and_plan():
    source = version()
    row_id = source.rows[0].row_id
    plan = repair(source, [RepairOperation("set_cell", [row_id], "Quantity", "3")])
    original = preview_repair(source, plan)
    changed_reason = preview_repair(source, replace(plan, reason="Different verified source"))
    assert original.fingerprint != changed_reason.fingerprint
    altered_row = replace(source.rows[0], values={**source.rows[0].values, "Price": "9"})
    altered_source = replace(source, rows=[altered_row])
    assert preview_repair(altered_source, plan).fingerprint != original.fingerprint


@pytest.mark.parametrize("operation", [
    RepairOperation("execute_python", value="print(1)"),
    RepairOperation("set_cell", ["unknown"], "Quantity", "2"),
    RepairOperation("change_settings", settings={"unknown": "value"}),
    RepairOperation("change_settings", settings={"date_format": "%d/%m/%y"}),
    RepairOperation("exclude_rows", []),
])
def test_invalid_operations_fail_without_mutating_parent(operation):
    source = version()
    original = source.to_dict()
    with pytest.raises(ValidationError):
        preview_repair(source, repair(source, [operation]))
    assert source.to_dict() == original


def test_unknown_column_empty_reason_stale_base_and_noop_are_rejected():
    source = version()
    row_id = source.rows[0].row_id
    plan = repair(source, [RepairOperation("set_cell", [row_id], "Quantity", "3")])
    with pytest.raises(ConflictError):
        preview_repair(source, replace(plan, base_version_id="older"))
    with pytest.raises(ValidationError):
        preview_repair(source, replace(plan, reason=" \n"))
    with pytest.raises(ValidationError):
        preview_repair(source, repair(source, [RepairOperation("set_cell", [row_id], "not-a-column", "3")]))
    with pytest.raises(ValidationError, match="no changes"):
        preview_repair(source, repair(source, [RepairOperation("set_cell", [row_id], "Quantity", "2")]))


def test_untargeted_cells_stay_identical_after_single_cell_repair():
    source = version([
        ["1", "A", "2", "3", "2024-01-01", "α"],
        ["2", "B", "-1", "5", "2024-01-02", "β"],
    ])
    plan = repair(source, [RepairOperation("set_cell", [source.rows[0].row_id], "Quantity", "4")])
    preview = preview_repair(source, plan)
    differences = [(before.row_id, column) for before, after in zip(source.rows, preview.version.rows, strict=True)
                   for column in source.columns if before.values[column] != after.values[column]]
    assert differences == [(source.rows[0].row_id, "Quantity")]
    assert Decimal(preview.after.metrics.net_amount) == Decimal(preview.before.metrics.net_amount) + Decimal(preview.amount_delta)


def test_dates_are_parsed_once_per_distinct_value(monkeypatch):
    actual_datetime = engine.datetime
    calls = []

    class CountingDatetime:
        @staticmethod
        def strptime(value, format):
            calls.append(value)
            return actual_datetime.strptime(value, format)

    monkeypatch.setattr(engine, "datetime", CountingDatetime)
    source = version([[str(i), "A", "1", "2", "2024-01-01", ""] for i in range(200)])
    assert analyze(source).metrics.amount_rows == 200
    assert calls == ["2024-01-01"]
