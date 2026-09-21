from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from data_detective.engine import analyze
from data_detective.exports import audit_csv, data_csv, investigation_report
from data_detective.models import (
    ColumnMap,
    ConflictError,
    ParseSettings,
    RepairOperation,
    RepairPlan,
    ValidationError,
)
from data_detective.store import Store

SOURCE = b"order,product,qty,price,date\nA,P,2,0.10,2024-01-02\nA,Q,1,3.00,2024-01-02\nC1,P,-1,0.10,2024-02-03\n"
MAP = ColumnMap("order", "product", "qty", "price", "date")
SETTINGS = ParseSettings("%Y-%m-%d")


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path)
    version = store.create_dataset("Sales", SOURCE, MAP, SETTINGS, import_key="import")
    return store, version


def plan_for(version, value="4.00"):
    return RepairPlan(version.version_id,
                      [RepairOperation("set_cell", [version.rows[1].row_id], "price", value)],
                      "Correct the documented unit price")


def test_import_retry_and_original_survive_restart(setup):
    store, version = setup
    again = store.create_dataset("Sales", SOURCE, MAP, SETTINGS, import_key="import")
    assert again == version
    restarted = Store(store.root)
    assert restarted.active_version(version.dataset_id) == version
    assert restarted.original_bytes(version.dataset_id) == SOURCE
    assert len(restarted.list_datasets()) == 1
    with pytest.raises(ConflictError):
        store.create_dataset("Different", SOURCE, MAP, SETTINGS, import_key="import")


def test_preview_has_no_persistent_side_effect(setup):
    store, version = setup
    preview = store.preview(version.dataset_id, plan_for(version))
    assert preview.amount_delta == "1.00" or float(preview.amount_delta) == 1
    assert store.active_version(version.dataset_id) == version
    assert len(store.history(version.dataset_id)) == 1


def test_idempotency_and_stale_preview(setup):
    store, version = setup
    plan = plan_for(version)
    preview = store.preview(version.dataset_id, plan)
    committed = store.apply(version.dataset_id, plan, preview.fingerprint, "action-1")
    assert store.apply(version.dataset_id, plan, preview.fingerprint, "action-1") == committed
    assert len(store.history(version.dataset_id)) == 2
    assert store.load_version(version.version_id) == version
    assert committed.rows[0] == version.rows[0]
    assert committed.rows[2] == version.rows[2]
    with pytest.raises(ConflictError):
        store.apply(version.dataset_id, plan, preview.fingerprint, "action-2")
    with pytest.raises(ConflictError):
        store.apply(version.dataset_id, plan_for(version, "7"), preview.fingerprint, "action-1")


def test_tampered_preview_refused(setup):
    store, version = setup
    plan = plan_for(version)
    preview = store.preview(version.dataset_id, plan)
    with pytest.raises(ConflictError):
        store.apply(version.dataset_id, plan_for(version, "99"), preview.fingerprint, "tampered")
    assert store.active_version(version.dataset_id) == version


def test_racing_repairs_commit_only_one(setup):
    store, version = setup
    plans = [plan_for(version, value) for value in ("4", "5")]
    previews = [store.preview(version.dataset_id, p) for p in plans]

    def attempt(index):
        try:
            store.apply(version.dataset_id, plans[index], previews[index].fingerprint, f"race-{index}")
            return "applied"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, [0, 1])) == ["applied", "conflict"]
    assert len(store.history(version.dataset_id)) == 2


def test_restore_is_new_version_with_exact_original_content(setup):
    store, version = setup
    plan = RepairPlan(version.version_id, [RepairOperation("exclude_rows", [version.rows[0].row_id])],
                      "Confirmed repeated import")
    preview = store.preview(version.dataset_id, plan)
    changed = store.apply(version.dataset_id, plan, preview.fingerprint, "exclude")
    restored = store.restore(version.dataset_id, version.version_id, changed.version_id,
                             "Restore source for comparison", "restore")
    assert restored.rows == version.rows
    assert restored.settings == version.settings
    assert restored.parent_version_id == changed.version_id
    assert restored.version_id != version.version_id
    assert len(store.history(version.dataset_id)) == 3
    assert store.restore(version.dataset_id, version.version_id, changed.version_id,
                         "Restore source for comparison", "restore") == restored
    assert analyze(restored).metrics.net_amount == analyze(version).metrics.net_amount


def test_restore_cannot_cross_datasets(setup):
    store, version = setup
    other = store.create_dataset("Other", SOURCE, MAP, SETTINGS)
    with pytest.raises(ValidationError):
        store.restore(version.dataset_id, other.version_id, version.version_id, "Mistake", "x")


def test_corrupt_snapshot_detected(setup):
    store, version = setup
    path = store.root / store.history(version.dataset_id)[0]["path"]
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValidationError, match="checksum"):
        store.load_version(version.version_id)


def test_publication_failure_never_commits_metadata(setup, monkeypatch):
    store, version = setup
    plan = plan_for(version)
    preview = store.preview(version.dataset_id, plan)

    def crash(_version):
        raise OSError("Disk full")

    monkeypatch.setattr(store, "_publish", crash)
    with pytest.raises(OSError):
        store.apply(version.dataset_id, plan, preview.fingerprint, "failed")
    assert Store(store.root).active_version(version.dataset_id) == version
    assert len(store.history(version.dataset_id)) == 1


def test_exports_include_settings_and_changes(setup):
    store, version = setup
    plan = plan_for(version)
    preview = store.preview(version.dataset_id, plan)
    changed = store.apply(version.dataset_id, plan, preview.fingerprint, "export")
    assert "4.00" in data_csv(changed).decode("utf-8-sig")
    assert "3.00,4.00" in audit_csv(store, version.dataset_id).decode("utf-8-sig")
    report = investigation_report(store, version.dataset_id)
    assert changed.version_id in report
    assert "not accounting revenue" in report
    assert "original" not in json.loads(store.history(version.dataset_id)[0]["changes_json"])[0]
