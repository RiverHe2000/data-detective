"""Framework-level UI tests exercising real state changes in an isolated workspace."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.proto.WidgetStates_pb2 import WidgetState
from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.element_tree import ElementTree

from data_detective import ui
from data_detective.models import ColumnMap, ParseSettings, RepairOperation, RepairPlan
from data_detective.store import Store

APP = Path(__file__).resolve().parents[1] / "app.py"
SAVE_CONFIRMATION = "I reviewed the affected rows and the result. Save this as a new version."
RESTORE_CONFIRMATION = "Restore these contents as a new version and preserve the current version."


@pytest.fixture(autouse=True)
def stateful_container_widgets(monkeypatch):
    """Bridge AppTest's missing Streamlit 1.64 tab/expander widget serialization.

    The browser sends these states on every interaction, but AppTest currently
    serializes neither container. Preserve their actual session-state values;
    leave any future native support alone. No application behavior is mocked.
    """
    native = ElementTree.get_widget_states

    def get_widget_states(tree):
        states = native(tree)
        serialized = {state.id for state in states.widgets}
        for node in tree:
            if node.type not in {"tab_container", "expander"}:
                continue
            widget_id = node.proto.id
            if not widget_id or widget_id in serialized:
                continue
            value = tree._runner.session_state[widget_id]
            state = WidgetState(id=widget_id)
            if node.type == "tab_container":
                state.string_value = value
            else:
                state.bool_value = value
            states.widgets.append(state)
        return states

    monkeypatch.setattr(ElementTree, "get_widget_states", get_widget_states)


def by_label(elements, label):
    return next(item for item in elements if item.label == label)


def navigate(app, label):
    dataset_id = app.session_state["dataset_id"]
    app.session_state[f"tabs_{dataset_id}"] = label
    app.run()
    assert not app.exception


def launch(tmp_path, monkeypatch, name=None):
    monkeypatch.setenv("DATA_DETECTIVE_WORKSPACE", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    by_label(app.button, name or "Open case").click().run()
    assert not app.exception
    return app


def create_case(store, name, count, *, duplicate_pairs=False):
    rows = [f"O{i},P{i},1,2.50,2024-01-02" for i in range(count)]
    if duplicate_pairs:
        rows = [row for row in rows for _ in range(2)]
    source = ("order,product,qty,price,date\n" + "\n".join(rows) + "\n").encode()
    return store.create_dataset(name, source, ColumnMap("order", "product", "qty", "price", "date"),
                                ParseSettings("%Y-%m-%d"), import_key=name)


def stage_exclusion(app, row_id, reason):
    navigate(app, "Repair lab")
    by_label(app.multiselect, "Row references").set_value([row_id]).run()
    by_label(app.button, "Add to repair plan").click().run()
    by_label(app.text_input, "Why is this change justified?").set_value(reason).run()
    by_label(app.button, "Preview combined impact").click().run()
    assert not app.exception
    return app.session_state["preview"]


def test_demo_repair_save_restore_and_recommendation(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DETECTIVE_WORKSPACE", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    by_label(app.button, "Open case").click().run()
    assert not app.exception
    assert app.title[0].value == "Duplicate Dispatch"
    store = Store(tmp_path)
    dataset = store.list_datasets()[0]
    original = store.active_version(dataset["id"])
    by_label(app.button, "Suggest where to look").click().run()
    assert not app.exception
    assert app.session_state["recommendation"][1].mode == "rules"
    navigate(app, "Repair lab")
    row_id = original.rows[-1].row_id
    by_label(app.multiselect, "Row references").set_value([row_id]).run()
    by_label(app.button, "Add to repair plan").click().run()
    by_label(app.text_input, "Why is this change justified?").set_value("Review the known injected repeated record").run()
    by_label(app.button, "Preview combined impact").click().run()
    assert not app.exception
    assert store.active_version(dataset["id"]).version_id == original.version_id
    by_label(app.checkbox, "I reviewed the affected rows and the result. Save this as a new version.").check().run()
    by_label(app.button, "Confirm & save version").click().run()
    assert not app.exception
    changed = store.active_version(dataset["id"])
    assert changed.version_id != original.version_id
    assert changed.rows[-1].excluded
    navigate(app, "History & export")
    by_label(app.checkbox, "Restore these contents as a new version and preserve the current version.").check().run()
    by_label(app.button, "Restore version").click().run()
    assert not app.exception
    assert store.active_version(dataset["id"]).rows == original.rows
    assert len(store.history(dataset["id"])) == 3


def test_changed_reason_disables_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DETECTIVE_WORKSPACE", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    by_label(app.button, "Open case").click().run()
    row_id = Store(tmp_path).active_version(Store(tmp_path).list_datasets()[0]["id"]).rows[-1].row_id
    navigate(app, "Repair lab")
    by_label(app.multiselect, "Row references").set_value([row_id]).run()
    by_label(app.button, "Add to repair plan").click().run()
    by_label(app.text_input, "Why is this change justified?").set_value("Original reason").run()
    by_label(app.button, "Preview combined impact").click().run()
    by_label(app.checkbox, "I reviewed the affected rows and the result. Save this as a new version.").check().run()
    by_label(app.text_input, "Why is this change justified?").set_value("Changed after preview").run()
    assert by_label(app.button, "Confirm & save version").disabled
    assert not app.exception


def test_findings_after_first_forty_are_accessible_and_actionable(tmp_path, monkeypatch):
    store = Store(tmp_path)
    original = create_case(store, "Forty-five duplicate groups", 45, duplicate_pairs=True)
    app = launch(tmp_path, monkeypatch, "Forty-five duplicate groups")
    assert by_label(app.number_input, "Lead page").max == 6
    by_label(app.number_input, "Lead page").set_value(6).run()
    assert not app.exception
    assert any(item.value == "Source row numbers: 89, 90" for item in app.caption)
    last_action = [item for item in app.button if item.label == "Use these rows in repair lab"][-1]
    last_action.click().run()
    assert not app.exception
    assert app.session_state["focus_ids"] == [row.row_id for row in original.rows[-2:]]
    by_label(app.checkbox, "Select all 2 included rows in this lead").check().run()
    by_label(app.button, "Add to repair plan").click().run()
    assert app.session_state["pending_operations"][0].row_ids == [row.row_id for row in original.rows[-2:]]


def test_rows_after_five_hundred_can_be_browsed_and_repaired(tmp_path, monkeypatch):
    store = Store(tmp_path)
    original = create_case(store, "Large source", 550)
    app = launch(tmp_path, monkeypatch, "Large source")
    app.session_state[f"browse_{original.version_id}"] = True
    app.run()
    by_label(app.number_input, "Source page").set_value(6).run()
    assert not app.exception
    displayed = [item.value for item in app.dataframe if "Row reference" in item.value.columns]
    assert len(displayed) == 1
    assert displayed[0]["Row reference"].tolist() == [row.row_id for row in original.rows[500:]]

    navigate(app, "Repair lab")
    by_label(app.radio, "Choose rows").set_value("Source row numbers").run()
    by_label(app.text_input, "Source row numbers").set_value("501-503").run()
    by_label(app.button, "Add to repair plan").click().run()
    by_label(app.text_input, "Why is this change justified?").set_value("Confirmed duplicated source batch").run()
    by_label(app.button, "Preview combined impact").click().run()
    expected = [row.row_id for row in original.rows[500:503]]
    assert [change.row_id for change in app.session_state["preview"].changes] == expected
    assert store.active_version(original.dataset_id) == original
    by_label(app.checkbox, SAVE_CONFIRMATION).check().run()
    by_label(app.button, "Confirm & save version").click().run()
    assert not app.exception
    changed = store.active_version(original.dataset_id)
    assert [row.row_id for row in changed.rows if row.excluded] == expected
    assert len(store.history(original.dataset_id)) == 2


def test_repair_draft_survives_current_case_tab_and_other_case_navigation(tmp_path, monkeypatch):
    store = Store(tmp_path)
    first = create_case(store, "First case", 3)
    second = create_case(store, "Second case", 4)
    app = launch(tmp_path, monkeypatch, "First case")
    first_preview = stage_exclusion(app, first.rows[0].row_id, "First case evidence")

    def assert_first_draft():
        assert not app.exception
        assert app.session_state["pending_operations"] == first_preview.plan.operations
        assert app.session_state["preview"].fingerprint == first_preview.fingerprint
        assert by_label(app.text_input, "Why is this change justified?").value == "First case evidence"

    by_label(app.button, "First case").click().run()
    assert_first_draft()
    navigate(app, "Investigate")
    navigate(app, "Repair lab")
    assert_first_draft()
    by_label(app.button, "Second case").click().run()
    second_preview = stage_exclusion(app, second.rows[1].row_id, "Second case evidence")
    by_label(app.button, "First case").click().run()
    navigate(app, "Repair lab")
    assert_first_draft()
    by_label(app.button, "Second case").click().run()
    navigate(app, "Repair lab")
    assert app.session_state["pending_operations"] == second_preview.plan.operations
    assert app.session_state["preview"].fingerprint == second_preview.fingerprint
    assert by_label(app.text_input, "Why is this change justified?").value == "Second case evidence"
    assert store.active_version(first.dataset_id) == first
    assert store.active_version(second.dataset_id) == second


def test_exports_are_requested_once_per_saved_version(tmp_path, monkeypatch):
    spies = {}
    for name in ("data_csv", "audit_csv", "investigation_report", "findings_csv"):
        spies[name] = Mock(wraps=getattr(ui, name))
        monkeypatch.setattr(ui, name, spies[name])
    app = launch(tmp_path, monkeypatch)
    navigate(app, "Repair lab")
    by_label(app.text_input, "Why is this change justified?").set_value("Unsaved draft").run()
    navigate(app, "Investigate")
    by_label(app.text_input, "Find a lead").set_value("duplicate").run()
    assert all(spy.call_count == 0 for spy in spies.values())
    navigate(app, "History & export")
    assert all(spy.call_count == 0 for spy in spies.values())
    by_label(app.button, "Prepare export files").click().run()
    assert not app.exception
    expected_calls = {"data_csv": 2, "audit_csv": 1, "investigation_report": 1, "findings_csv": 1}
    assert {name: spy.call_count for name, spy in spies.items()} == expected_calls
    app.run()
    navigate(app, "Investigate")
    navigate(app, "Repair lab")
    navigate(app, "History & export")
    assert {name: spy.call_count for name, spy in spies.items()} == expected_calls
    assert len(app.get("download_button")) == 6


def test_restoring_different_target_or_reason_requires_new_confirmation(tmp_path, monkeypatch):
    store = Store(tmp_path)
    original = create_case(store, "Restore safety", 3)
    current = original
    versions = [original]
    for index in range(2):
        plan = RepairPlan(current.version_id,
                          [RepairOperation("exclude_rows", [current.rows[index].row_id])],
                          f"Confirmed repeated row {index + 1}")
        preview = store.preview(current.dataset_id, plan)
        current = store.apply(current.dataset_id, plan, preview.fingerprint, f"save-{index}")
        versions.append(current)
    app = launch(tmp_path, monkeypatch, "Restore safety")
    navigate(app, "History & export")
    by_label(app.selectbox, "Version to restore").set_value(versions[0].version_id).run()
    by_label(app.checkbox, RESTORE_CONFIRMATION).check().run()
    assert not by_label(app.button, "Restore version").disabled
    by_label(app.selectbox, "Version to restore").set_value(versions[1].version_id).run()
    assert not by_label(app.checkbox, RESTORE_CONFIRMATION).value
    assert by_label(app.button, "Restore version").disabled
    by_label(app.checkbox, RESTORE_CONFIRMATION).check().run()
    assert not by_label(app.button, "Restore version").disabled
    by_label(app.text_input, "Reason for restoring").set_value("Different documented reason").run()
    assert not by_label(app.checkbox, RESTORE_CONFIRMATION).value
    assert by_label(app.button, "Restore version").disabled
    assert not app.exception
    assert store.active_version(original.dataset_id) == current
    assert len(store.history(original.dataset_id)) == 3
