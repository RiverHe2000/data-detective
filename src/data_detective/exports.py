"""Plain data, a complete edit trail, and a report with computed figures."""

from __future__ import annotations

import csv
import io
import json

from .engine import analyze
from .models import Analysis, DatasetVersion, ValidationError
from .store import Store


def data_csv(version: DatasetVersion, *, include_row_ids: bool = False) -> bytes:
    out = io.StringIO(newline="")
    # csv's minimal quoting only protects characters in its line terminator.
    # CRLF therefore protects both lone CR and LF in original cell text.
    writer = csv.writer(out, lineterminator="\r\n")
    reference_column = "_detective_row_id"
    occupied = {c.strip() for c in version.columns}
    while reference_column in occupied:
        reference_column = "_" + reference_column
    writer.writerow(([reference_column] if include_row_ids else []) + version.columns)
    for row in version.rows:
        if not row.excluded:
            writer.writerow(([row.row_id] if include_row_ids else []) + [row.values[c] for c in version.columns])
    return out.getvalue().encode("utf-8-sig")


def _history_through(store: Store, dataset_id: str, version_id: str) -> list[dict]:
    return list(reversed(store.history(dataset_id, through_version_id=version_id)))


def audit_csv(store: Store, dataset_id: str, version_id: str | None = None) -> bytes:
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(["version_id", "parent_version_id", "created_at", "kind", "reason", "row_id",
                     "column", "before", "after", "restored_from"])
    selected = version_id or store.dataset(dataset_id)["active_version_id"]
    for item in _history_through(store, dataset_id, selected):
        changes = json.loads(item["changes_json"])
        plan = json.loads(item["plan_json"]) if item["plan_json"] else {}
        prefix = [item[k] or "" for k in ("id", "parent_version_id", "created_at", "kind", "reason")]
        for change in changes or [{}]:
            writer.writerow(prefix + [change.get(k, "") for k in ("row_id", "column", "before", "after")]
                            + [plan.get("restored_from", "")])
    return out.getvalue().encode("utf-8-sig")


def findings_csv(analysis: Analysis) -> bytes:
    """Export every finding-to-row association, without the report's display cap."""
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(["version_id", "finding_id", "rule_id", "severity", "title", "row_id", "column", "explanation"])
    for finding in analysis.findings:
        for row_id in finding.row_ids:
            writer.writerow([analysis.version_id, finding.finding_id, finding.rule_id, finding.severity,
                             finding.title, row_id, finding.column or "", finding.explanation])
    return out.getvalue().encode("utf-8-sig")


def _text(value: str) -> str:
    return value.replace("\n", " ").replace("\r", " ").replace("|", "\\|").replace("<", "&lt;")


def investigation_report(
    store: Store, dataset_id: str, version_id: str | None = None, *, analysis: Analysis | None = None,
) -> str:
    dataset = store.dataset(dataset_id)
    version = store.load_version(version_id or dataset["active_version_id"], dataset_id)
    if analysis is None or not getattr(analysis, "version_id", ""):
        # An analysis cached before version binding was added is safe to reload,
        # but cannot be trusted for reuse just because its metrics look similar.
        analysis = analyze(version)
    elif analysis.version_id != version.version_id:
        raise ValidationError("The supplied analysis belongs to a different version; recalculate the report.")
    metrics = analysis.metrics
    lines = [f"# {_text(dataset['name'])}", "", "## Confirmed calculation contract", "",
             f"- Dataset: `{dataset_id}`; exported version: `{version.version_id}`.",
             f"- Original SHA-256: `{dataset['source_sha256']}`.",
             "- Field mapping: " + "; ".join(f"{key} → `{_text(value)}`" for key, value in vars(version.mapping).items()) + ".",
             f"- Currency: {metrics.currency}; dates: `{version.settings.date_format}`.",
             f"- Decimal separator: `{version.settings.decimal_separator}`; thousands separator: `{version.settings.thousands_separator or '(none)'}`.",
             "- Net transaction amount is the signed sum of quantity × unit price; it is not accounting revenue.",
             "- Cancellations are not negated again. Excluded rows remain in the version history.",
             "", "## Calculated results", "",
             f"- Net transaction amount: **{metrics.currency} {metrics.net_amount}**.",
             f"- Included rows: {metrics.included_rows}; excluded rows: {metrics.excluded_rows}.",
             f"- Rows contributing to amount: {metrics.amount_rows}; unparseable amounts: {metrics.invalid_amount_rows}.",
             f"- Unassigned-month amount: {metrics.currency} {metrics.unassigned_amount}; invalid dates: {metrics.invalid_date_rows}.",
             "", "| Month | Net transaction amount |", "|---|---:|"]
    lines.extend(f"| {month} | {amount} |" for month, amount in metrics.monthly.items())
    lines += ["", "## Unresolved findings", "",
              "Review candidates are not confirmed business errors. AI recommendations do not establish causality.", ""]
    if not analysis.findings:
        lines.append("No findings under the implemented checks; this does not prove that the data are correct.")
    for finding in analysis.findings:
        lines += [f"### {_text(finding.title)}", "",
                  f"`{finding.finding_id}` · {finding.severity} · {len(finding.row_ids)} affected rows.",
                  _text(finding.explanation), "",
                  "Row references: " + ", ".join(f"`{r}`" for r in finding.row_ids[:30])
                  + (" (first 30; see application for all rows)" if len(finding.row_ids) > 30 else ""), ""]
    lines += ["## Version history", "", "| Version | Action | Reason |", "|---|---|---|"]
    for item in _history_through(store, dataset_id, version.version_id):
        lines.append(f"| `{item['id']}` | {item['kind']} | {_text(item['reason'])} |")
    lines += ["", "The exported change log records exact cell edits, row exclusions and setting changes.",
              "Processed CSV preserves source text. Reimport using the mapping and parse settings in this report.",
              "The optional row-reference column is named _detective_row_id, with extra leading underscores if that name already exists.",
              "Restores reference the immutable source version; they do not perform reverse arithmetic.", ""]
    return "\n".join(lines)
