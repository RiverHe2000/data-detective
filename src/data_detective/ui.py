"""The local investigation workbench. All business rules live outside this file."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict
from decimal import Decimal
from math import ceil
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .advisor import DEFAULT_MODEL, recommend, rules_recommend
from .engine import analyze, read_csv
from .explorer import filter_findings, filter_rows, select_source_rows
from .exports import audit_csv, data_csv, findings_csv, investigation_report
from .models import ColumnMap, ParseSettings, RepairOperation, RepairPlan, ValidationError
from .store import Store

PROJECT = Path(__file__).resolve().parents[2]
DATE_FORMATS = {
    "Year-month-day + time · 2024-03-08 14:30:00": "%Y-%m-%d %H:%M:%S",
    "Year-month-day · 2024-03-08": "%Y-%m-%d",
    "Day/month/year · 08/03/2024": "%d/%m/%Y",
    "Month/day/year · 03/08/2024": "%m/%d/%Y",
    "Day/month/year + time · 08/03/2024 14:30": "%d/%m/%Y %H:%M",
    "Month/day/year + time · 03/08/2024 14:30": "%m/%d/%Y %H:%M",
}
ALIASES = {"order_id": ["InvoiceNo", "order_id", "OrderID"], "product_id": ["StockCode", "product_id"],
           "quantity": ["Quantity", "quantity", "qty"], "unit_price": ["UnitPrice", "unit_price", "price"],
           "order_time": ["InvoiceDate", "order_time", "date"]}
LABELS = {"order_id": "Order number", "product_id": "Product code", "quantity": "Quantity",
          "unit_price": "Unit price", "order_time": "Transaction date"}


def money(value: str, currency: str = "") -> str:
    number = Decimal(value)
    # UI rounds for readability; exact values are displayed below and in the report.
    return f"{currency} {number:,.2f}".strip()


@st.cache_data(show_spinner=False, max_entries=12)
def inspect_version(root: str, version_id: str):
    return analyze(Store(root).load_version(version_id))


def open_dataset(dataset_id: str | None, *, reset: bool = False):
    previous = st.session_state.get("dataset_id")
    if previous == dataset_id and not reset:
        return
    retained = st.session_state.setdefault("case_sessions", {})
    fields = ("pending_operations", "operations_base", "preview", "preview_key", "recommendation", "focus_ids", "spotlight")
    if previous and not reset:
        retained[previous] = {key: st.session_state[key] for key in fields if key in st.session_state}
    if reset:
        retained.pop(dataset_id, None)
    st.session_state.dataset_id = dataset_id
    for key in fields:
        st.session_state.pop(key, None)
    if dataset_id in retained:
        st.session_state.update(retained[dataset_id])


def page_window(count: int, size: int, key: str, label: str) -> tuple[int, int]:
    pages = max(1, ceil(count / size))
    if pages == 1:
        st.caption(f"{count:,} {'record' if count == 1 else 'records'} · all shown")
        return 0, size
    current = min(int(st.session_state.get(key, 1)), pages)
    st.session_state[key] = current
    page = st.number_input(label, min_value=1, max_value=pages, step=1, key=key, disabled=pages == 1)
    start = (page - 1) * size
    st.caption(f"{start + 1 if count else 0:,}–{min(start + size, count):,} of {count:,} · page {page} / {pages}")
    return start, start + size


def show_finding(finding, version, *, spotlight=False):
    suffix = f"spotlight_{finding.finding_id}" if spotlight else finding.finding_id
    with st.expander(f"{finding.title} · {len(finding.row_ids):,} rows", expanded=spotlight):
        st.caption("CHECK REQUIRED" if finding.severity == "error" else "REVIEW CANDIDATE")
        st.write(finding.explanation)
        start, end = page_window(len(finding.row_ids), 50, f"evidence_{suffix}", "Evidence page")
        refs = finding.row_ids[start:end]
        st.caption("Source row numbers: " + ", ".join(r.rsplit(":", 1)[-1] for r in refs))
        st.dataframe(row_table(version, refs), hide_index=True, width="stretch",
                     column_config={c: st.column_config.TextColumn(width="small") for c in asdict(version.mapping).values()})
        if st.button("Use these rows in repair lab", key=f"focus_{suffix}"):
            st.session_state.focus_ids = finding.row_ids
            st.session_state.navigate_tab = "Repair lab"
            st.rerun()


def row_table(version, row_ids=None, limit=200):
    wanted = set(row_ids) if row_ids is not None else None
    occupied = {c.strip() for c in version.columns}
    reference, included = "Row reference", "Included"
    while reference in occupied:
        reference = "System " + reference
    while included in occupied:
        included = "System " + included
    records = []
    first_columns = list(asdict(version.mapping).values())
    for row in version.rows:
        if wanted is not None and row.row_id not in wanted:
            continue
        records.append({**{column: row.values[column] for column in first_columns}, **row.values,
                        reference: row.row_id, included: not row.excluded})
        if len(records) >= limit:
            break
    return pd.DataFrame(records)


def settings_inputs(settings: ParseSettings | None, prefix: str) -> ParseSettings:
    settings = settings or ParseSettings()
    labels = list(DATE_FORMATS)
    custom = settings.date_format not in DATE_FORMATS.values()
    index = list(DATE_FORMATS.values()).index(settings.date_format) if not custom else len(labels)
    date_label = st.selectbox("Date format", labels + ["Custom explicit format"], index=index, key=f"{prefix}_date")
    date_format = DATE_FORMATS.get(date_label)
    if date_format is None:
        date_format = st.text_input("Date format pattern", settings.date_format, key=f"{prefix}_custom_date")
    left, mid, right = st.columns(3)
    currencies = ["GBP", "AUD", "USD", "EUR", "CNY"]
    if settings.currency not in currencies:
        currencies.append(settings.currency)
    with left:
        currency = st.selectbox("Currency", currencies,
                               index=currencies.index(settings.currency) if settings.currency in currencies else 0,
                               key=f"{prefix}_currency")
    with mid:
        decimal = st.selectbox("Decimal mark", [".", ","], index=[".", ","].index(settings.decimal_separator),
                               key=f"{prefix}_decimal")
    separators = ["", ",", ".", " ", "'"]
    if settings.thousands_separator not in separators:
        separators.append(settings.thousands_separator)
    with right:
        thousands = st.selectbox("Thousands separator", separators,
                                 index=separators.index(settings.thousands_separator) if settings.thousands_separator in separators else 0,
                                 format_func=lambda x: "None" if not x else ("Space" if x == " " else x),
                                 key=f"{prefix}_thousands")
    return ParseSettings(date_format, currency, decimal, thousands)


def import_form(store):
    upload = st.file_uploader("Upload a sales CSV", type=["csv"], help="UTF-8 CSV, up to 20 MiB and 50,000 rows.")
    if upload is None:
        st.caption("One file · one currency · your data stays on this computer")
        return
    data = upload.getvalue()
    signature = hashlib.sha256(data).hexdigest()[:16]
    separator = st.selectbox("CSV separator", [",", ";", "\t"], format_func=lambda x: {",": "Comma", ";": "Semicolon", "\t": "Tab"}[x])
    try:
        columns, rows = read_csv(data, separator)
    except ValidationError as exc:
        st.error(str(exc))
        return
    st.dataframe(pd.DataFrame([r.values for r in rows[:8]]), hide_index=True, width="stretch")
    st.caption(f"{len(rows):,} rows · {len(columns)} columns. Confirm what the fields mean before investigating.")
    with st.form(f"import_{signature}"):
        name = st.text_input("Investigation name", Path(upload.name).stem[:150])
        mapped = {}
        cols = st.columns(3)
        for i, field in enumerate(ALIASES):
            guess = next((name for name in ALIASES[field] if name in columns), columns[min(i, len(columns) - 1)])
            with cols[i % 3]:
                mapped[field] = st.selectbox(LABELS[field], columns, index=columns.index(guess), key=f"map_{signature}_{field}")
        settings = settings_inputs(None, f"import_{signature}")
        confirmed = st.checkbox("I have confirmed the field mapping, currency and parsing formats.")
        submitted = st.form_submit_button("Start investigation", type="primary")
    if submitted:
        if not confirmed:
            st.warning("Confirm the data contract above before importing.")
            return
        try:
            version = store.create_dataset(name, data, ColumnMap(**mapped), settings, separator,
                                           import_key=f"{st.session_state.session_nonce}-{signature}-{hashlib.sha256(json.dumps([mapped,asdict(settings),name,separator],sort_keys=True).encode()).hexdigest()}")
            open_dataset(version.dataset_id)
            st.rerun()
        except ValidationError as exc:
            st.error(str(exc))


def case_catalog():
    order = ["dev-duplicate-dispatch", "dev-broken-dates", "dev-price-spike"]
    files = [PROJECT / "data" / "cases" / slug / "manifest.json" for slug in order]
    cases = []
    for file in files:
        if not file.is_file():
            continue
        meta = json.loads(file.read_text("utf-8"))
        if meta.get("split", "dev") == "dev":
            cases.append((file.parent, meta))
    return cases[:3]


def home(store):
    st.markdown('<div class="eyebrow">FOLLOW THE EVIDENCE</div>', unsafe_allow_html=True)
    st.title("Every number has a story.")
    st.markdown("Find the records behind a surprising result. Try a repair, see what changes, and keep every step.")
    st.write("")
    cases = case_catalog()
    if cases:
        st.subheader("Open a sample investigation")
        descriptions = [
            ("01", "A suspicious sales jump", "Trace repeated records and compare the effect of excluding them."),
            ("02", "A month out of place", "Investigate date parsing and see the difference between timing and amount."),
            ("03", "The unexpected price", "Inspect a surprising price without assuming it must be wrong."),
        ]
        for col, (path, meta), (number, title, body) in zip(st.columns(3), cases, descriptions, strict=False):
            with col, st.container(border=True):
                st.caption(f"CASE {number} / UCI RETAIL")
                st.markdown(f"### {meta.get('title', title)}")
                st.write(meta.get("description", body))
                st.caption("Public source data with documented injected issues.")
                if st.button("Open case", key=f"demo_{path.name}", width="stretch"):
                    mapping = ColumnMap("InvoiceNo", "StockCode", "Quantity", "UnitPrice", "InvoiceDate")
                    version = store.create_dataset(meta.get("title", title), (path / "input.csv").read_bytes(),
                                                   mapping, ParseSettings(),
                                                   import_key=f"demo-{path.name}-{st.session_state.session_nonce}")
                    open_dataset(version.dataset_id)
                    st.rerun()
    st.write("")
    st.subheader("Start with your own data")
    with st.container(border=True):
        import_form(store)
    st.caption("Investigate → preview → confirm. An unusual record is a lead, not automatically an error.")


def chart_months(before, after=None):
    months = sorted(set(before.monthly) | (set(after.monthly) if after else set()))
    if not months:
        st.info("No amounts have a valid month under the selected date format.")
        return
    fig = go.Figure()
    fig.add_bar(name="Current" if after else "Net transaction amount", x=months,
                y=[float(before.monthly.get(m, "0")) for m in months], marker_color="#087f74")
    if after:
        fig.add_bar(name="Preview", x=months, y=[float(after.monthly.get(m, "0")) for m in months], marker_color="#e6aa50")
    fig.update_layout(height=250, margin=dict(l=0, r=8, t=12, b=0), barmode="group", paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", font=dict(color="#526575"),
                      legend=dict(orientation="h", y=1.2), yaxis_title=before.currency,
                      xaxis=dict(type="category"), yaxis=dict(gridcolor="#e5e9eb"))
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def investigation(store, version, analysis):
    metrics = analysis.metrics
    cols = st.columns(4)
    cols[0].metric("NET AMOUNT", money(metrics.net_amount, metrics.currency))
    cols[1].metric("AMOUNT COVERAGE", f"{metrics.amount_rows:,} / {metrics.included_rows:,}")
    cols[2].metric("UNASSIGNED MONTH", money(metrics.unassigned_amount, metrics.currency))
    cols[3].metric("OPEN LEADS", len(analysis.findings))
    st.caption("Amounts above are rounded to two decimals for display. Exact values are retained in reports. Refund signs are preserved.")
    if metrics.invalid_amount_rows:
        st.warning(f"{metrics.invalid_amount_rows:,} included rows have an unparseable quantity or price and are not in the amount total.")
    left, right = st.columns([1.45, 1], gap="large")
    with left:
        st.subheader("Follow the leads")
        error_count = sum(f.severity == "error" for f in analysis.findings)
        st.caption(f"{error_count:,} checks required · {len(analysis.findings) - error_count:,} review candidates")
        selected_rule = st.selectbox("Show", ["All checks", "Errors", "Review candidates"], label_visibility="collapsed")
        search = st.text_input("Find a lead", placeholder="Rule, field, or exact source row number")
        findings = filter_findings(analysis.findings, selected_rule, search)
        spotlight = next((f for f in analysis.findings if f.finding_id == st.session_state.get("spotlight")), None)
        if spotlight:
            st.caption("RECOMMENDED LEAD")
            show_finding(spotlight, version, spotlight=True)
            if st.button("Close recommended lead"):
                st.session_state.pop("spotlight", None)
                st.rerun()
        if not findings:
            st.success("No findings in this view. The implemented checks do not prove that every record is correct.")
        start, end = page_window(len(findings), 8, f"leads_{version.version_id}_{selected_rule}_{search}", "Lead page")
        for finding in findings[start:end]:
            show_finding(finding, version)
        browse = st.expander("Browse source rows", key=f"browse_{version.version_id}", on_change="rerun")
        if browse.open:
            with browse:
                query = st.text_input("Search all source rows", placeholder="Order, product, source row number, or any cell text")
                status = st.selectbox("Include", ["All rows", "Included rows", "Excluded rows"])
                matches = filter_rows(version.rows, query, status)
                start, end = page_window(len(matches), 100, f"browse_page_{version.version_id}_{query}_{status}", "Source page")
                st.dataframe(row_table(version, [r.row_id for r in matches[start:end]]), hide_index=True, width="stretch")
                st.caption("Search covers the entire dataset. Use stable source row numbers in the repair lab.")
    with right:
        st.subheader("Ask the investigation assistant")
        questions = st.session_state.setdefault("questions", {})
        question = st.text_area("What looks surprising?", questions.get(version.dataset_id, "Why has the reported sales amount changed?"), max_chars=1000,
                                height=95, key=f"question_{version.dataset_id}")
        questions[version.dataset_id] = question
        ai = st.toggle("Use the local AI model", value=False, help="Runs only on this computer. The first call loads the model.")
        if st.button("Suggest where to look", type="primary", width="stretch"):
            with st.spinner("Reviewing the available evidence…"):
                rec = recommend(question, analysis.findings,
                                model_path=os.environ.get("DATA_DETECTIVE_MODEL_PATH", DEFAULT_MODEL)) if ai else rules_recommend(question, analysis.findings)
            st.session_state.recommendation = (version.version_id, rec, question)
        saved = st.session_state.get("recommendation")
        if saved and saved[0] == version.version_id:
            rec = saved[1]
            with st.container(border=True):
                st.caption(f"{rec.mode.upper()} · {rec.elapsed_seconds:.1f}s")
                if rec.mode == "qwen_cached":
                    st.caption("Reused a validated local suggestion for this same question and evidence.")
                if len(saved) > 2 and saved[2] != question:
                    st.info("This suggestion belongs to the previous question. Submit again to update it.")
                if rec.fallback_reason:
                    st.info(f"Using the rule-based guide: {rec.fallback_reason}")
                st.write(rec.explanation)
                lookup = {f.finding_id: f for f in analysis.findings}
                for fid in rec.finding_ids:
                    if fid in lookup:
                        st.markdown(f"**{lookup[fid].title}**")
                        st.caption("Source rows: " + ", ".join(r.rsplit(":", 1)[-1] for r in lookup[fid].row_ids[:12]))
                        if st.button("Inspect this lead", key=f"inspect_{fid}"):
                            st.session_state.spotlight = fid
                            st.rerun()
                for step in rec.next_steps:
                    st.write(f"• {step}")
                st.caption("Recommendations are hypotheses. Inspect the records before changing anything.")
        st.write("")
        st.subheader("Amount by transaction month")
        chart_months(metrics)
        with st.expander("Calculation contract"):
            st.write("Net transaction amount = signed quantity × unit price. This is not accounting revenue.")
            st.json({"mapping": asdict(version.mapping), "parsing": asdict(version.settings),
                     "exact_net_amount": metrics.net_amount, "exact_unassigned_amount": metrics.unassigned_amount})
            st.caption("Checks completed in " + f"{analysis.elapsed_seconds:.3f}s.")


def repair_lab(store, version):
    st.subheader("Try a repair before changing the data")
    st.write("Build one or more operations, compare the combined result, then decide whether to save it.")
    st.caption("Draft plans stay with their investigation during this browser session. Only confirmed versions survive a restart.")
    ops = st.session_state.setdefault("pending_operations", [])
    if ops and st.session_state.get("operations_base") != version.version_id:
        ops = []
        st.session_state.pending_operations = ops
        st.session_state.pop("preview", None)
        st.info("The dataset changed. Start a fresh repair plan.")
    st.session_state.operations_base = version.version_id
    left, right = st.columns([1, 1.1], gap="large")
    with left, st.container(border=True):
        operation_label = st.selectbox("Operation", ["Exclude selected rows", "Correct a cell value", "Trim outer whitespace", "Change parsing settings"])
        operation = None
        if operation_label == "Change parsing settings":
            settings = settings_inputs(version.settings, f"repair_{version.version_id}")
            st.caption("Currency is a label, not a currency conversion. Confirm the source currency before changing it.")
            operation = RepairOperation("change_settings", settings=asdict(settings))
        else:
            available = {r.row_id: r for r in version.rows if not r.excluded}
            focused = [r for r in st.session_state.get("focus_ids", []) if r in available]
            mode = st.radio("Choose rows", ["Search rows", "Source row numbers"], horizontal=True)
            selected = []
            if mode == "Source row numbers":
                source_rows = st.text_input("Source row numbers", placeholder="27, 31, 501-505", max_chars=2000)
                try:
                    selected = select_source_rows(version.rows, source_rows)
                except ValidationError as exc:
                    st.warning(str(exc))
            else:
                query = st.text_input("Find rows to repair", placeholder="Search every included row")
                candidates = filter_rows([available[rid] for rid in focused] if focused else available.values(), query, "Included rows")
                limit_rows = [row.row_id for row in candidates[:200]]
                st.caption(f"{len(candidates):,} matching rows" + (" in the selected lead." if focused else " across the dataset.")
                           + (" Refine the search or enter source row numbers to reach the remaining rows." if len(candidates) > 200 else ""))
                selected = st.multiselect("Row references", limit_rows, default=[],
                                          format_func=lambda rid: f"Row {rid.rsplit(':', 1)[-1]} · {available[rid].values[version.mapping.order_id]} / {available[rid].values[version.mapping.product_id]}",
                                          key=f"rows_{version.version_id}_{hashlib.sha256(','.join(limit_rows).encode()).hexdigest()[:10]}")
                if focused and st.checkbox(f"Select all {len(focused):,} included rows in this lead"):
                    selected = focused
            if selected:
                st.caption(f"{len(selected):,} source {'row' if len(selected) == 1 else 'rows'} selected. The operation will apply to every selected row.")
            if focused and st.button("Clear lead selection"):
                st.session_state.pop("focus_ids", None)
                st.rerun()
            if operation_label == "Exclude selected rows":
                st.caption("Rows remain in history. Repeated order numbers and returns can be legitimate.")
                operation = RepairOperation("exclude_rows", selected)
            else:
                column = st.selectbox("Column", version.columns)
                if operation_label == "Correct a cell value":
                    value = st.text_input("Documented replacement value")
                    st.caption("The same explicit value is applied to every selected cell. No values are inferred.")
                    operation = RepairOperation("set_cell", selected, column, value)
                else:
                    operation = RepairOperation("normalize_whitespace", selected, column)
        if st.button("Add to repair plan", width="stretch"):
            if operation.kind != "change_settings" and not operation.row_ids:
                st.warning("Select at least one row.")
            elif len(ops) >= 20:
                st.warning("A plan can contain at most 20 operations.")
            else:
                ops.append(operation)
                st.session_state.pop("preview", None)
                st.rerun()
    with right:
        st.markdown("#### Your repair plan")
        if not ops:
            st.info("Add an operation to begin. The original data stays intact.")
        for i, op in enumerate(ops):
            labels = {"set_cell": "Replace cell value", "exclude_rows": "Exclude rows",
                      "normalize_whitespace": "Trim whitespace", "change_settings": "Change parsing settings"}
            st.write(f"**{i + 1}. {labels[op.kind]}**" + (f" · {len(op.row_ids)} {'row' if len(op.row_ids) == 1 else 'rows'}" if op.row_ids else ""))
            if op.row_ids:
                st.caption("Source rows: " + ", ".join(r.rsplit(":", 1)[-1] for r in op.row_ids[:30])
                           + (" …" if len(op.row_ids) > 30 else ""))
            if op.column:
                st.caption(f"Field: {op.column}" + (f" → {op.value!r}" if op.kind == "set_cell" else " · trim leading and trailing whitespace"))
            if op.settings:
                st.caption("Dates: " + op.settings.get("date_format", version.settings.date_format)
                           + " · currency: " + op.settings.get("currency", version.settings.currency))
            if st.button("Remove operation", key=f"remove_operation_{i}"):
                ops.pop(i)
                st.session_state.pop("preview", None)
                st.rerun()
        reasons = st.session_state.setdefault("draft_reasons", {})
        reason = st.text_input("Why is this change justified?", value=reasons.get(version.version_id, ""),
                               max_chars=1000, key=f"reason_{version.version_id}")
        reasons[version.version_id] = reason
        pcol, ccol = st.columns(2)
        if pcol.button("Preview combined impact", type="primary", disabled=not ops, width="stretch"):
            try:
                plan = RepairPlan(version.version_id, list(ops), reason.strip())
                with st.spinner("Recalculating the full dataset…"):
                    preview = store.preview(version.dataset_id, plan)
                st.session_state.preview = preview
                st.session_state.preview_key = uuid.uuid4().hex
            except ValidationError as exc:
                st.error(str(exc))
        if ccol.button("Clear plan", disabled=not ops, width="stretch"):
            st.session_state.pending_operations = []
            st.session_state.pop("preview", None)
            st.rerun()
    preview = st.session_state.get("preview")
    if preview is None or preview.plan.base_version_id != version.version_id:
        return
    st.divider()
    st.markdown("### Preview · nothing has been saved yet")
    if preview.plan.reason != reason.strip():
        st.warning("The reason changed since this preview. Rebuild the preview before saving.")
    cols = st.columns(3)
    cols[0].metric("CURRENT AMOUNT", money(preview.before.metrics.net_amount, version.settings.currency))
    cols[1].metric("PREVIEW AMOUNT", money(preview.after.metrics.net_amount, preview.after.metrics.currency),
                   delta=money(preview.amount_delta), delta_color="off")
    cols[2].metric("ROWS WITH VALID AMOUNTS", preview.after.metrics.amount_rows,
                   delta=preview.after.metrics.amount_rows - preview.before.metrics.amount_rows, delta_color="off")
    st.caption(f"Exact amount change: {preview.amount_delta}. Unassigned-month amount: {preview.before.metrics.unassigned_amount} → {preview.after.metrics.unassigned_amount}.")
    before_error_rows = {rid for finding in preview.before.findings if finding.severity == "error" for rid in finding.row_ids}
    after_error_rows = {rid for finding in preview.after.findings if finding.severity == "error" for rid in finding.row_ids}
    newly_flagged = after_error_rows - before_error_rows
    if newly_flagged:
        st.warning(f"This plan introduces error flags on {len(newly_flagged):,} previously unflagged source rows. Review them before saving.")
    st.dataframe(pd.DataFrame([
        {"Coverage": "Rows with error findings", "Current": len(before_error_rows), "Preview": len(after_error_rows)},
        {"Coverage": "Invalid amount rows", "Current": preview.before.metrics.invalid_amount_rows, "Preview": preview.after.metrics.invalid_amount_rows},
        {"Coverage": "Unparsed date rows", "Current": preview.before.metrics.invalid_date_rows, "Preview": preview.after.metrics.invalid_date_rows},
        {"Coverage": "Excluded rows", "Current": preview.before.metrics.excluded_rows, "Preview": preview.after.metrics.excluded_rows},
    ]), hide_index=True, width="stretch")
    chart_months(preview.before.metrics, preview.after.metrics)
    start, end = page_window(len(preview.changes), 50, f"changes_{preview.fingerprint}", "Changes page")
    st.dataframe(pd.DataFrame([{"Source row": c.row_id.rsplit(":", 1)[-1] if c.row_id != "__settings__" else "All rows",
                                "Field": "Excluded from totals" if c.column == "__excluded__" else c.column,
                                "Before": c.before, "After": c.after}
                               for c in preview.changes[start:end]]), hide_index=True, width="stretch")
    st.caption(f"{len(preview.changes):,} recorded {'change' if len(preview.changes) == 1 else 'changes'}. The entire plan is recalculated together.")
    confirmed = st.checkbox("I reviewed the affected rows and the result. Save this as a new version.",
                             key=f"confirm_{preview.fingerprint}")
    if st.button("Confirm & save version", type="primary", disabled=not confirmed or reason.strip() != preview.plan.reason):
        try:
            store.apply(version.dataset_id, preview.plan, preview.fingerprint, st.session_state.preview_key)
            open_dataset(version.dataset_id, reset=True)
            st.session_state.flash = "New version saved. The original and all earlier versions are preserved."
            st.rerun()
        except (ValidationError, OSError) as exc:
            st.error(str(exc))


def history_export(store, version, analysis):
    st.subheader("Every step is kept")
    history = store.history(version.dataset_id)
    display = [{"Version": item["id"][:10], "Saved at": item["created_at"], "Action": item["kind"],
                "Reason": item["reason"], "Current": item["id"] == version.version_id} for item in history]
    st.dataframe(pd.DataFrame(display), hide_index=True, width="stretch")
    with st.expander("Restore an earlier version"):
        choices = [item for item in history if item["id"] != version.version_id]
        if choices:
            by_id = {item["id"]: item for item in choices}
            target = st.selectbox("Version to restore", list(by_id),
                                  format_func=lambda key: f"{key[:10]} · {by_id[key]['reason']}")
            target_version = store.load_version(target, version.dataset_id)
            target_analysis = inspect_version(str(store.root), target)
            st.write("Restored net amount: **" + money(target_analysis.metrics.net_amount, target_version.settings.currency) + "**")
            reason = st.text_input("Reason for restoring", "Return to an earlier investigation state", max_chars=1000)
            confirmation_key = f"restore_confirm_{version.version_id}_{target}_{hashlib.sha256(reason.strip().encode()).hexdigest()}"
            confirm = st.checkbox("Restore these contents as a new version and preserve the current version.", key=confirmation_key)
            if st.button("Restore version", disabled=not confirm or not reason.strip()):
                try:
                    store.restore(version.dataset_id, target, version.version_id, reason.strip(),
                                  f"restore-{version.version_id}-{target}-{hashlib.sha256(reason.encode()).hexdigest()}")
                    open_dataset(version.dataset_id, reset=True)
                    st.session_state.flash = "Version restored. History is preserved."
                    st.rerun()
                except ValidationError as exc:
                    st.error(str(exc))
        else:
            st.info("This investigation still contains only its original import.")
    st.write("")
    st.subheader("Take the evidence with you")
    st.write("The processed CSV contains included rows. The report records parsing settings, unresolved findings and exact amounts.")
    bundle = st.session_state.get("export_bundle")
    cache_key = (str(store.root), version.version_id)
    if not bundle or bundle[0] != cache_key:
        st.caption("Files are prepared only when requested, for the currently saved version.")
        if st.button("Prepare export files", type="primary"):
            with st.spinner("Preparing this version's evidence…"):
                bundle = (cache_key, {
                    "data": data_csv(version),
                    "audit": audit_csv(store, version.dataset_id, version.version_id),
                    "report": investigation_report(store, version.dataset_id, version.version_id, analysis=analysis),
                    "findings": findings_csv(analysis),
                    "original": store.original_bytes(version.dataset_id),
                    "references": data_csv(version, include_row_ids=True),
                })
                st.session_state.export_bundle = bundle
            st.rerun()
        return
    files = bundle[1]
    st.caption(f"Ready for saved version {version.version_id[:10]}. Every unresolved finding and affected row is included in the findings CSV.")
    cols = st.columns(3)
    cols[0].download_button("↓ Processed data", files["data"], "processed-data.csv", "text/csv", width="stretch", on_click="ignore")
    cols[1].download_button("↓ Complete change log", files["audit"], "change-log.csv", "text/csv", width="stretch", on_click="ignore")
    cols[2].download_button("↓ Investigation report", files["report"], "investigation-report.md", "text/markdown", width="stretch", on_click="ignore")
    st.download_button("↓ All findings and source rows", files["findings"], "findings.csv", "text/csv", on_click="ignore")
    with st.expander("Original file and row references"):
        st.download_button("Download original CSV", files["original"], "original.csv", "text/csv", on_click="ignore")
        st.download_button("Download processed CSV with row references", files["references"],
                           "processed-with-references.csv", "text/csv", on_click="ignore")


def main():
    st.set_page_config(page_title="Data Detective", page_icon="◈", layout="wide")
    st.markdown("""<style>
    .stApp {font-family: 'Segoe UI', sans-serif;}
    .block-container {padding-top:4.3rem; max-width:1440px;}
    h1 {letter-spacing:-1.4px; font-weight:700!important;}
    h2,h3 {letter-spacing:-.45px;}
    .eyebrow {font-size:11px; letter-spacing:2.4px; color:#087f74; font-weight:700; margin-bottom:8px;}
    [data-testid="stMetric"] {background:#fff; padding:18px 20px; border:1px solid #e2e8eb; border-radius:12px;}
    [data-testid="stMetricLabel"] {font-size:10px; letter-spacing:1px; color:#677c89;}
    [data-testid="stMetricValue"] {font-size:22px;letter-spacing:-.4px;}
    [data-testid="stSidebar"] {border-right:1px solid #dce5e7;}
    [data-testid="stExpander"] {background:#fff;}
    [data-testid="stTabs"] button {font-weight:600;}
    .brand {font-size:23px;font-weight:750;letter-spacing:-.8px;color:#087f74;}
    [data-testid="stAppDeployButton"] {display:none;}
    </style>""", unsafe_allow_html=True)
    st.session_state.setdefault("session_nonce", uuid.uuid4().hex)
    store = Store(os.environ.get("DATA_DETECTIVE_WORKSPACE", str(PROJECT / "workspace")))
    with st.sidebar:
        st.markdown('<div class="brand">◈ Data Detective</div>', unsafe_allow_html=True)
        st.caption("A clearer trail from data to decisions.")
        st.write("")
        if st.button("＋ New investigation", width="stretch", type="primary"):
            open_dataset(None)
            st.rerun()
        st.write("")
        st.caption("YOUR INVESTIGATIONS")
        for item in store.list_datasets():
            if st.button(item["name"], key=f"case_{item['id']}", width="stretch"):
                open_dataset(item["id"])
                st.rerun()
        st.divider()
        st.caption("LOCAL WORKSPACE")
        st.write("Originals preserved · full version history")
        st.caption("Checks find evidence. You decide what needs changing.")
    dataset_id = st.session_state.get("dataset_id")
    try:
        if not dataset_id:
            home(store)
            return
        dataset = store.dataset(dataset_id)
        version = store.active_version(dataset_id)
        analysis = inspect_version(str(store.root), version.version_id)
        st.markdown('<div class="eyebrow">YOUR INVESTIGATION</div>', unsafe_allow_html=True)
        st.title(dataset["name"])
        st.caption(f"{len(version.rows):,} source rows · {version.settings.currency} · version {version.version_id[:10]} · saved {version.created_at[:16].replace('T',' ')} UTC")
        if st.session_state.get("flash"):
            st.success(st.session_state.pop("flash"))
        tab_key = f"tabs_{version.dataset_id}"
        if st.session_state.get("navigate_tab"):
            st.session_state[tab_key] = st.session_state.pop("navigate_tab")
        tabs = st.tabs(["Investigate", "Repair lab", "History & export"], key=tab_key, on_change="rerun")
        if tabs[0].open:
            with tabs[0]:
                investigation(store, version, analysis)
        if tabs[1].open:
            with tabs[1]:
                repair_lab(store, version)
        if tabs[2].open:
            with tabs[2]:
                history_export(store, version, analysis)
    except (ValidationError, OSError) as exc:
        st.error(str(exc))
        st.info("The original data has been retained. Return to the investigation list or inspect the local workspace.")
