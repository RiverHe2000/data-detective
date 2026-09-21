"""Cross-boundary regressions found during independent product review."""

from __future__ import annotations

import csv
import io
from dataclasses import asdict, replace

import pytest
from streamlit.testing.v1 import AppTest

from data_detective.engine import analyze, read_csv
from data_detective.exports import audit_csv, data_csv, investigation_report
from data_detective.models import ColumnMap, ParseSettings, RepairOperation, RepairPlan
from data_detective.store import Store

MAPPING = ColumnMap("order", "product", "qty", "price", "date")
SETTINGS = ParseSettings("%Y-%m-%d")
SOURCE = b"order,product,qty,price,date\nA,P,1,5,2024-01-02\n"


def price_plan(version, price="7"):
    return RepairPlan(version.version_id,
                      [RepairOperation("set_cell", [version.rows[0].row_id], "price", price)],
                      "Price verified against the source")


def test_duplicate_request_committed_between_lookup_and_preview_returns_same_result(tmp_path, monkeypatch):
    store = Store(tmp_path)
    other_connection = Store(tmp_path)
    version = store.create_dataset("Sales", SOURCE, MAPPING, SETTINGS)
    plan = price_plan(version)
    preview = store.preview(version.dataset_id, plan)
    original_lookup = store._prior_request
    already_interleaved = False
    committed = []

    def commit_between_lookup_and_preview(conn, dataset_id, key, payload_sha):
        nonlocal already_interleaved
        prior = original_lookup(conn, dataset_id, key, payload_sha)
        if not already_interleaved and prior is None:
            already_interleaved = True
            committed.append(other_connection.apply(dataset_id, plan, preview.fingerprint, key))
        return prior

    monkeypatch.setattr(store, "_prior_request", commit_between_lookup_and_preview)
    returned = store.apply(version.dataset_id, plan, preview.fingerprint, "same-action")
    assert len(committed) == 1
    assert returned.version_id == committed[0].version_id
    assert len(store.history(version.dataset_id)) == 2


def test_exported_report_and_audit_stay_on_selected_version_after_concurrent_save(tmp_path):
    store = Store(tmp_path)
    version = store.create_dataset("Sales", SOURCE, MAPPING, SETTINGS)
    captured_csv = data_csv(version)
    plan = price_plan(version)
    preview = store.preview(version.dataset_id, plan)
    latest = store.apply(version.dataset_id, plan, preview.fingerprint, "later-save")
    report = investigation_report(store, version.dataset_id, version.version_id)
    audit = audit_csv(store, version.dataset_id, version.version_id).decode("utf-8-sig")
    columns, rows = read_csv(captured_csv)
    exported = replace(version, columns=columns, rows=rows)
    assert analyze(exported).metrics.net_amount == "5.00"
    assert "GBP 5.00" in report
    assert latest.version_id not in report
    assert latest.version_id not in audit
    assert version.version_id in report
    assert version.version_id in audit


def test_report_contains_complete_column_mapping_for_reimport(tmp_path):
    store = Store(tmp_path)
    version = store.create_dataset("Sales", SOURCE, MAPPING, SETTINGS)
    report = investigation_report(store, version.dataset_id, version.version_id)
    for semantic, source_column in asdict(MAPPING).items():
        assert semantic in report
        assert source_column in report


@pytest.mark.parametrize("existing_column", ["_detective_row_id", " _detective_row_id ", "__detective_row_id"])
def test_row_reference_export_never_duplicates_an_existing_header(tmp_path, existing_column):
    source = f"order,product,qty,price,date,{existing_column}\nA,P,1,5,2024-01-02,source-value\n".encode()
    store = Store(tmp_path)
    version = store.create_dataset("Sales", source, MAPPING, SETTINGS)
    columns, rows = read_csv(data_csv(version, include_row_ids=True))
    assert len(columns) == len(version.columns) + 1
    assert len({c.strip() for c in columns}) == len(columns)
    assert rows[0].values[existing_column] == "source-value"
    reference_column = next(c for c in columns if c not in version.columns)
    assert rows[0].values[reference_column] == version.rows[0].row_id
    assert analyze(replace(version, columns=columns, rows=rows)).metrics.net_amount == "5.00"


def test_parse_settings_ui_preserves_existing_currency_and_nbsp_across_reruns():
    app = AppTest.from_string("""
from dataclasses import asdict
import streamlit as st
from data_detective.models import ParseSettings
from data_detective.ui import settings_inputs
value = settings_inputs(ParseSettings('%Y-%m-%d', 'NZD', ',', '\\u00a0'), 'existing')
st.session_state['returned_settings'] = asdict(value)
""")
    app.run(timeout=15)
    assert not app.exception
    assert app.selectbox(key="existing_currency").value == "NZD"
    assert app.selectbox(key="existing_thousands").value == "\u00a0"
    date_widget = app.selectbox(key="existing_date")
    changed_date = next(option for option in date_widget.options if option.startswith("Day/month/year ·"))
    date_widget.select(changed_date).run(timeout=15)
    assert not app.exception
    stored = app.session_state["returned_settings"]
    assert stored["date_format"] == "%d/%m/%Y"
    assert stored["currency"] == "NZD"
    assert stored["thousands_separator"] == "\u00a0"
    assert stored["decimal_separator"] == ","


def test_source_columns_cannot_overwrite_system_row_reference_or_inclusion_status(tmp_path):
    from data_detective.ui import row_table

    store = Store(tmp_path)
    source = (b"order,product,qty,price,date,Row reference,Included\n"
              b"A,P,1,5,2024-01-02,source-reference,source-status\n")
    version = store.create_dataset("Sales", source, MAPPING, SETTINGS)
    displayed = row_table(version).to_dict("records")[0]
    assert displayed["Row reference"] == "source-reference"
    assert displayed["Included"] == "source-status"
    assert version.rows[0].row_id in displayed.values()
    assert any(value is True for value in displayed.values())


def test_restored_export_reproduces_exact_amount_and_keeps_restore_reference(tmp_path):
    store = Store(tmp_path)
    source = b"order,product,qty,price,date\nA,P,2,0.1,2024-01-02\nC1,P,-1,0.01,invalid\n"
    original = store.create_dataset("Sales", source, MAPPING, SETTINGS)
    plan = RepairPlan(original.version_id, [RepairOperation("exclude_rows", [original.rows[0].row_id])],
                      "Temporarily isolate the refund")
    preview = store.preview(original.dataset_id, plan)
    altered = store.apply(original.dataset_id, plan, preview.fingerprint, "exclude")
    restored = store.restore(original.dataset_id, original.version_id, altered.version_id,
                             "Return to the original evidence", "restore")
    columns, rows = read_csv(data_csv(restored))
    reimported = replace(restored, columns=columns, rows=rows)
    assert analyze(reimported).metrics == analyze(restored).metrics
    assert analyze(reimported).metrics.net_amount == "0.19"
    entries = list(csv.DictReader(io.StringIO(audit_csv(store, original.dataset_id, restored.version_id).decode("utf-8-sig"))))
    restore_entry = next(entry for entry in entries if entry["kind"] == "restore")
    assert restore_entry["version_id"] == restored.version_id
    assert restore_entry["parent_version_id"] == altered.version_id
    assert restore_entry["restored_from"] == original.version_id
