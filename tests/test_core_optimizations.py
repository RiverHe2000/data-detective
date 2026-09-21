"""Regression tests for lossless exports and consistent, bounded core operations."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, replace

import pytest

import data_detective.exports as exports
from data_detective.engine import analyze, preview_repair, read_csv
from data_detective.models import (
    ColumnMap,
    DataRow,
    DatasetVersion,
    ParseSettings,
    RepairOperation,
    RepairPlan,
    ValidationError,
)
from data_detective.store import Store, canonical, digest

COLUMNS = ["order", "product", "qty", "price", "date", "note"]
MAPPING = ColumnMap(*COLUMNS[:5])
SETTINGS = ParseSettings("%Y-%m-%d")


def source_csv(note="original"):
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    writer.writerow(["C1", "P", "-1", "0.125", "2024-01-02", note])
    return out.getvalue().encode("utf-8")


def in_memory_version(note="original"):
    columns, rows = read_csv(source_csv(note))
    return DatasetVersion("v1", "d1", None, columns, rows, MAPPING, SETTINGS)


@pytest.mark.parametrize("note", ["one\rtwo", "one\ntwo", "one\r\ntwo", 'one,"two"\rthree'])
def test_export_roundtrip_preserves_every_newline_and_decimal_amount(note):
    version = in_memory_version(note)
    columns, rows = read_csv(exports.data_csv(version))
    restored = replace(version, columns=columns, rows=rows)
    assert restored.rows[0].values == version.rows[0].values
    assert analyze(restored).metrics == analyze(version).metrics
    assert analyze(restored).metrics.net_amount == "-0.125"


def test_audit_csv_preserves_carriage_returns_in_reason_before_and_after(tmp_path):
    store = Store(tmp_path)
    version = store.create_dataset("Sales", source_csv("old\rnote"), MAPPING, SETTINGS)
    plan = RepairPlan(version.version_id,
                      [RepairOperation("set_cell", [version.rows[0].row_id], "note", "new\r\nnote")],
                      "Verified\ragain")
    preview = store.preview(version.dataset_id, plan)
    changed = store.apply(version.dataset_id, plan, preview.fingerprint, "edit")
    rows = list(csv.DictReader(io.StringIO(exports.audit_csv(store, version.dataset_id).decode("utf-8-sig"), newline="")))
    repaired = next(row for row in rows if row["version_id"] == changed.version_id)
    assert repaired["reason"] == "Verified\ragain"
    assert repaired["before"] == "old\rnote"
    assert repaired["after"] == "new\r\nnote"
    assert None not in repaired


def test_faster_snapshot_serialization_retains_format_hash_and_detached_containers():
    version = in_memory_version()
    data = version.to_dict()
    assert data == asdict(version)
    assert digest(canonical(data)) == digest(canonical(asdict(version)))
    assert DatasetVersion.from_dict(data) == version
    data["rows"][0]["values"]["note"] = "changed outside the snapshot"
    data["rows"].append(data["rows"][0])
    data["columns"].append("other")
    data["mapping"]["quantity"] = "other"
    data["settings"]["currency"] = "AUD"
    assert version.rows[0].values["note"] == "original"
    assert len(version.rows) == 1
    assert version.columns == COLUMNS
    assert version.mapping.quantity == "qty"
    assert version.settings.currency == "GBP"


def test_preview_does_not_share_mutable_plan_selections_with_caller():
    version = in_memory_version()
    selected = [version.rows[0].row_id]
    plan = RepairPlan(version.version_id, [RepairOperation("set_cell", selected, "qty", "-2")], "Verified")
    preview = preview_repair(version, plan)
    selected.clear()
    plan.operations.append(RepairOperation("exclude_rows", [version.rows[0].row_id]))
    assert len(preview.plan.operations) == 1
    assert preview.plan.operations[0].row_ids == [version.rows[0].row_id]
    assert preview.version.rows[0].excluded is False
    assert preview.version.rows[0].values["qty"] == "-2"


def test_committed_audit_cannot_be_rewritten_by_caller_mutation_during_publication(tmp_path, monkeypatch):
    store = Store(tmp_path)
    version = store.create_dataset("Sales", source_csv(), MAPPING, SETTINGS)
    selected = [version.rows[0].row_id]
    plan = RepairPlan(version.version_id, [RepairOperation("set_cell", selected, "qty", "-2")], "Verified")
    preview = store.preview(version.dataset_id, plan)
    publish = store._publish

    def change_original_request_then_publish(snapshot):
        selected.clear()
        plan.operations.append(RepairOperation("exclude_rows", [version.rows[0].row_id]))
        return publish(snapshot)

    monkeypatch.setattr(store, "_publish", change_original_request_then_publish)
    changed = store.apply(version.dataset_id, plan, preview.fingerprint, "edit")
    saved_plan = json.loads(store.history(version.dataset_id)[0]["plan_json"])
    assert len(saved_plan["operations"]) == 1
    assert saved_plan["operations"][0]["row_ids"] == [version.rows[0].row_id]
    assert changed.rows[0].values["qty"] == "-2"
    assert changed.rows[0].excluded is False


@pytest.mark.parametrize("data", [
    None, [], {}, {"base_version_id": "v1", "reason": "verified", "operations": "edit"},
    {"base_version_id": "v1", "reason": "verified", "operations": [None]},
    {"base_version_id": "v1", "reason": "verified", "operations": [{"kind": "set_cell", "row_ids": "row"}]},
    {"base_version_id": "v1", "reason": "verified", "operations": [{"kind": "set_cell", "extra": "ignored before"}]},
    {"base_version_id": "v1", "reason": "verified", "operations": [{"kind": "set_cell", "value": 1}]},
    {"base_version_id": "v1", "reason": "verified", "operations": [{"kind": "change_settings", "settings": []}]},
])
def test_malformed_json_repair_plans_raise_user_validation_errors(data):
    with pytest.raises(ValidationError):
        RepairPlan.from_dict(data)


def test_history_is_bounded_to_selected_version_and_stays_within_dataset(tmp_path):
    store = Store(tmp_path)
    first = store.create_dataset("Sales", source_csv(), MAPPING, SETTINGS)
    plan = RepairPlan(first.version_id, [RepairOperation("set_cell", [first.rows[0].row_id], "qty", "-2")], "Verified")
    preview = store.preview(first.dataset_id, plan)
    latest = store.apply(first.dataset_id, plan, preview.fingerprint, "edit")
    assert [row["id"] for row in store.history(first.dataset_id, through_version_id=first.version_id)] == [first.version_id]
    assert [row["id"] for row in store.history(first.dataset_id)] == [latest.version_id, first.version_id]
    other = store.create_dataset("Other", source_csv(), MAPPING, SETTINGS)
    with pytest.raises(ValidationError):
        store.history(first.dataset_id, through_version_id=other.version_id)


def test_report_reuses_only_matching_analysis_and_reads_snapshot_once(tmp_path, monkeypatch):
    store = Store(tmp_path)
    version = store.create_dataset("Sales", source_csv(), MAPPING, SETTINGS)
    analysis = analyze(version)
    assert analysis.version_id == version.version_id
    loads = []
    load_version = store.load_version

    def record_load(*args, **kwargs):
        loads.append(args[0])
        return load_version(*args, **kwargs)

    def unexpected_reanalysis(_version):
        raise AssertionError("The confirmed analysis should be reused")

    monkeypatch.setattr(store, "load_version", record_load)
    monkeypatch.setattr(exports, "analyze", unexpected_reanalysis)
    report = exports.investigation_report(store, version.dataset_id, version.version_id, analysis=analysis)
    assert "GBP -0.125" in report
    assert loads == [version.version_id]
    with pytest.raises(ValidationError, match="different version"):
        exports.investigation_report(store, version.dataset_id, analysis=replace(analysis, version_id="other"))


def test_legacy_cached_analysis_without_version_binding_is_recalculated(tmp_path):
    store = Store(tmp_path)
    version = store.create_dataset("Sales", source_csv(), MAPPING, SETTINGS)
    analysis = analyze(version)
    old_cached_value = replace(analysis, version_id="", metrics=replace(analysis.metrics, net_amount="999.00"))
    report = exports.investigation_report(store, version.dataset_id, analysis=old_cached_value)
    assert "GBP -0.125" in report
    assert "GBP 999.00" not in report


def test_findings_csv_includes_all_row_references_and_version_without_truncation():
    original = in_memory_version()
    rows = [DataRow(str(index), {**original.rows[0].values, "order": str(index), "qty": "invalid"})
            for index in range(75)]
    version = replace(original, rows=rows)
    analysis = analyze(version)
    records = list(csv.DictReader(io.StringIO(exports.findings_csv(analysis).decode("utf-8-sig"), newline="")))
    assert len(records) == 75
    assert {record["row_id"] for record in records} == {row.row_id for row in rows}
    assert {record["version_id"] for record in records} == {version.version_id}
    assert {record["rule_id"] for record in records} == {"numeric_parse"}
