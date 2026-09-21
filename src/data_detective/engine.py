"""Deterministic CSV checks and side-effect-free, version-bound repair previews.

The engine never executes model text. All money is calculated with Decimal;
missing or invalid cells remain visible instead of silently becoming zero.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from .models import (
    MAX_BYTES,
    MAX_ROWS,
    REQUIRED_FIELDS,
    Analysis,
    CellChange,
    ColumnMap,
    ConflictError,
    DataRow,
    DatasetVersion,
    Finding,
    Metrics,
    ParseSettings,
    Preview,
    RepairPlan,
    ValidationError,
)

MAX_QUANTITY = Decimal("1000000000")
MAX_PRICE = Decimal("1000000000000")
MAX_FRACTION_DIGITS = 12
_SETTING_FIELDS = frozenset(asdict(ParseSettings()))
_DATE_DIRECTIVES = frozenset("YmdHMSf")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _columns(columns: list[str]) -> None:
    if not columns or any(not isinstance(c, str) or not c.strip() for c in columns):
        raise ValidationError("CSV headers must be nonempty text.")
    if any("\x00" in c for c in columns):
        raise ValidationError("CSV headers cannot contain NUL characters.")
    if len({c.strip() for c in columns}) != len(columns):
        raise ValidationError("CSV headers must be unique, including after trimming outer whitespace.")
    if "__excluded__" in columns:
        raise ValidationError("The header __excluded__ is reserved for the repair audit trail.")


def _validate_quoting(content: str, delimiter: str) -> None:
    """Reject quote characters in unquoted cells (csv.reader otherwise accepts them)."""
    state = "start"
    line = 1
    for char in content:
        if state == "quoted":
            if char == '"':
                state = "closed"
        elif state == "closed":
            if char == '"':
                state = "quoted"  # Escaped quote inside the same quoted field.
            elif char == delimiter or char in "\r\n":
                state = "start"
            else:
                raise ValidationError(f"Malformed CSV: unexpected text after a closing quote near line {line}.")
        elif char == '"':
            if state != "start":
                raise ValidationError(f"Malformed CSV: a quote occurs inside an unquoted field near line {line}.")
            state = "quoted"
        elif char == delimiter or char in "\r\n":
            state = "start"
        else:
            state = "plain"
        if char == "\n":
            line += 1
    if state == "quoted":
        raise ValidationError(f"Malformed CSV: unclosed quoted field near line {line}.")


def read_csv(data: bytes, delimiter: str = ",") -> tuple[list[str], list[DataRow]]:
    """Read UTF-8 (optional BOM), preserving raw strings and physical record order.

    A row ID is the full source-byte SHA-256 plus its one-based data-record index.
    Quoted newlines remain part of one record. Ragged and blank records fail.
    """
    if not isinstance(data, bytes):
        raise ValidationError("CSV input must be bytes.")
    if len(data) > MAX_BYTES:
        raise ValidationError("CSV exceeds the 20 MiB upload limit.")
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in {'"', "\r", "\n", "\x00"}:
        raise ValidationError("Choose one CSV delimiter other than a quote or line break.")
    try:
        content = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValidationError("CSV must use UTF-8 encoding; UTF-8 with BOM is also accepted.") from exc
    if "\x00" in content:
        raise ValidationError("CSV contains a NUL character and cannot be interpreted safely.")
    _validate_quoting(content, delimiter)
    source_sha = hashlib.sha256(data).hexdigest()
    # csv's default 128 KiB field limit is smaller than the documented upload cap.
    csv.field_size_limit(MAX_BYTES)
    reader = csv.reader(io.StringIO(content, newline=""), delimiter=delimiter, strict=True)
    rows: list[DataRow] = []
    try:
        headers = next(reader, None)
        if headers is None:
            raise ValidationError("CSV is empty; a header row is required.")
        _columns(headers)
        for index, values in enumerate(reader, start=1):
            if index > MAX_ROWS:
                raise ValidationError("CSV exceeds the 50,000 data-row limit.")
            if len(values) != len(headers):
                raise ValidationError(
                    f"CSV record {index} (physical line {reader.line_num}) has {len(values)} fields; "
                    f"the header has {len(headers)}."
                )
            rows.append(DataRow(f"{source_sha}:{index}", dict(zip(headers, values, strict=True))))
    except csv.Error as exc:
        raise ValidationError(f"Malformed CSV near physical line {reader.line_num}: {exc}") from exc
    return headers, rows


def _date_format(date_format: str) -> None:
    if not isinstance(date_format, str) or not date_format or len(date_format) > 100:
        raise ValidationError("Provide an explicit date format of at most 100 characters.")
    directives: list[str] = []
    position = 0
    while position < len(date_format):
        char = date_format[position]
        if char in "\r\n\x00":
            raise ValidationError("Date formats cannot contain line breaks or NUL characters.")
        if char == "%":
            position += 1
            if position >= len(date_format) or date_format[position] not in _DATE_DIRECTIVES:
                raise ValidationError("Use numeric date directives %Y, %m, %d and optional %H, %M, %S, %f.")
            directives.append(date_format[position])
        position += 1
    if any(directives.count(token) != 1 for token in "Ymd"):
        raise ValidationError("Date format must contain exactly one %Y, %m and %d; dates are never guessed.")
    if len(set(directives)) != len(directives):
        raise ValidationError("Date format directives cannot be repeated.")


def validate_contract(columns: list[str], mapping: ColumnMap, settings: ParseSettings) -> None:
    """Validate the user's explicit mapping and single-currency parse contract."""
    _columns(columns)
    if not isinstance(mapping, ColumnMap) or not isinstance(settings, ParseSettings):
        raise ValidationError("A column mapping and parse settings are required.")
    mapped = [getattr(mapping, name) for name in REQUIRED_FIELDS]
    if any(not isinstance(column, str) or column not in columns for column in mapped):
        raise ValidationError("Every required field must map to an existing CSV column.")
    if len(set(mapped)) != len(mapped):
        raise ValidationError("Map the five required fields to five different columns.")
    if not isinstance(settings.currency, str) or not re.fullmatch(r"[A-Z]{3}", settings.currency):
        raise ValidationError("Currency must be one three-letter uppercase code, such as GBP; no conversion is performed.")
    if not isinstance(settings.decimal_separator, str) or settings.decimal_separator not in {".", ","}:
        raise ValidationError("Decimal separator must be a dot or comma.")
    if not isinstance(settings.thousands_separator, str) or settings.thousands_separator not in {"", ".", ",", " ", "'", "\u00a0"}:
        raise ValidationError("Unsupported thousands separator.")
    if settings.thousands_separator == settings.decimal_separator:
        raise ValidationError("Decimal and thousands separators must differ.")
    _date_format(settings.date_format)


def _validate_version(version: DatasetVersion) -> None:
    validate_contract(version.columns, version.mapping, version.settings)
    if len(version.rows) > MAX_ROWS:
        raise ValidationError("A version cannot contain more than 50,000 source rows.")
    ids: set[str] = set()
    expected = set(version.columns)
    for row in version.rows:
        if not isinstance(row.row_id, str) or not row.row_id or row.row_id in ids or row.row_id == "__settings__":
            raise ValidationError("Each source row needs a unique, nonempty, nonreserved row ID.")
        ids.add(row.row_id)
        if set(row.values) != expected or any(not isinstance(value, str) for value in row.values.values()):
            raise ValidationError(f"Row {row.row_id} does not match the CSV column contract.")
        if not isinstance(row.excluded, bool):
            raise ValidationError("Row exclusion must be a boolean.")


def _number_pattern(settings: ParseSettings) -> re.Pattern[str]:
    integer = r"[0-9]+"
    if settings.thousands_separator:
        grouped = rf"[0-9]{{1,3}}(?:{re.escape(settings.thousands_separator)}[0-9]{{3}})+"
        integer = rf"(?:{integer}|{grouped})"
    return re.compile(rf"[+-]?{integer}(?:{re.escape(settings.decimal_separator)}[0-9]+)?\Z")


def _date_pattern(date_format: str) -> re.Pattern[str]:
    """Separate compact fields at fixed widths; delimited day/month may be unpadded."""
    parts: list[str] = []
    position = 0
    while position < len(date_format):
        if date_format[position] != "%":
            parts.append(re.escape(date_format[position]))
            position += 1
            continue
        directive = date_format[position + 1]
        adjacent = (position >= 2 and date_format[position - 2] == "%") or (
            position + 2 < len(date_format) and date_format[position + 2] == "%"
        )
        if directive == "Y":
            parts.append(r"[0-9]{4}")
        elif directive == "f":
            parts.append(r"[0-9]{1,6}")
        else:
            parts.append(r"[0-9]{2}" if adjacent else r"[0-9]{1,2}")
        position += 2
    return re.compile("".join(parts) + r"\Z")


def _number(raw: str, field: str, settings: ParseSettings, pattern: re.Pattern[str]) -> tuple[Decimal | None, str]:
    text = raw.strip()
    if not text:
        return None, "missing"
    if len(text) > 128 or pattern.fullmatch(text) is None:
        return None, "Number does not match the confirmed decimal/thousands format; exponent notation is not accepted."
    normalized = text.replace(settings.thousands_separator, "") if settings.thousands_separator else text
    normalized = normalized.replace(settings.decimal_separator, ".")
    if "." in normalized and len(normalized.rsplit(".", 1)[1]) > MAX_FRACTION_DIGITS:
        return None, f"At most {MAX_FRACTION_DIGITS} decimal places are supported."
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None, "Invalid decimal number."
    limit = MAX_QUANTITY if field == "quantity" else MAX_PRICE
    if not value.is_finite() or abs(value) > limit:
        return None, f"Absolute {field} must not exceed {format(limit, 'f')}."
    if field == "quantity" and value != value.to_integral_value():
        return None, "Quantity must be an integer value."
    return value, ""


def _money(value: Decimal) -> str:
    """No rounding: retain significant decimals and display at least two places."""
    if value == 0:
        return "0.00"
    text = format(value, "f")
    if "." not in text:
        return text + ".00"
    integer, fraction = text.split(".")
    return integer + "." + fraction.rstrip("0").ljust(2, "0")


def _finding(
    rule_id: str,
    severity: str,
    title: str,
    row_ids: list[str],
    column: str | None,
    evidence: dict[str, Any],
    explanation: str,
) -> Finding:
    identity = {"rule": rule_id, "rows": row_ids, "column": column, "evidence": evidence}
    finding_id = "f_" + hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()[:24]
    return Finding(finding_id, rule_id, severity, title, row_ids, column, evidence, explanation)


def _quartile(ordered: list[Decimal], numerator: int) -> Decimal:
    """Linear interpolation, matching the usual inclusive sample quantile."""
    index, remainder = divmod((len(ordered) - 1) * numerator, 4)
    if not remainder:
        return ordered[index]
    return ordered[index] + (ordered[index + 1] - ordered[index]) * Decimal(remainder) / Decimal(4)


def analyze(version: DatasetVersion) -> Analysis:
    """Evaluate included rows; review findings never exclude or mutate anything."""
    started = time.perf_counter()
    _validate_version(version)
    pattern = _number_pattern(version.settings)
    date_pattern = _date_pattern(version.settings.date_format)
    missing: dict[str, list[DataRow]] = defaultdict(list)
    invalid_numbers: dict[str, list[tuple[DataRow, str]]] = defaultdict(list)
    invalid_dates: list[DataRow] = []
    date_cache: dict[str, datetime | None] = {}
    numeric_cache: dict[tuple[str, str], tuple[Decimal | None, str]] = {}
    duplicates: dict[tuple[str, ...], list[str]] = defaultdict(list)
    prices: dict[str, list[tuple[str, Decimal]]] = defaultdict(list)
    monthly: dict[str, Decimal] = defaultdict(Decimal)
    total = Decimal(0)
    unassigned = Decimal(0)
    included_count = amount_count = invalid_date_count = 0

    with localcontext() as context:
        context.prec = 60
        for row in version.rows:
            if row.excluded:
                continue
            included_count += 1
            for field in REQUIRED_FIELDS:
                if not row.values[getattr(version.mapping, field)].strip():
                    missing[field].append(row)
            parsed: dict[str, Decimal | None] = {}
            for field in ("quantity", "unit_price"):
                raw = row.values[getattr(version.mapping, field)]
                cache_key = (field, raw)
                if cache_key not in numeric_cache:
                    numeric_cache[cache_key] = _number(raw, field, version.settings, pattern)
                value, reason = numeric_cache[cache_key]
                parsed[field] = value
                if value is None and reason != "missing":
                    invalid_numbers[field].append((row, reason))

            raw_date = row.values[version.mapping.order_time].strip()
            if raw_date not in date_cache:
                try:
                    date_cache[raw_date] = (
                        datetime.strptime(raw_date, version.settings.date_format)
                        if raw_date and date_pattern.fullmatch(raw_date) else None
                    )
                except ValueError:
                    date_cache[raw_date] = None
            parsed_date = date_cache[raw_date]
            if parsed_date is None:
                invalid_date_count += 1
                if raw_date:
                    invalid_dates.append(row)
            quantity, price = parsed["quantity"], parsed["unit_price"]
            if quantity is not None and price is not None:
                amount = quantity * price
                amount_count += 1
                total += amount
                if parsed_date is None:
                    unassigned += amount
                else:
                    monthly[f"{parsed_date.year:04d}-{parsed_date.month:02d}"] += amount
            product = row.values[version.mapping.product_id]
            if product.strip() and price is not None:
                prices[product].append((row.row_id, price))
            duplicates[tuple(row.values[column] for column in version.columns)].append(row.row_id)

        findings: list[Finding] = []
        for field in REQUIRED_FIELDS:
            affected = missing.get(field, [])
            if affected:
                column = getattr(version.mapping, field)
                findings.append(_finding(
                    "missing_required", "error", f"Required values are missing: {column}",
                    [row.row_id for row in affected], column,
                    {"field": field, "count": len(affected), "examples": [r.values[column] for r in affected[:5]]},
                    "Blank required cells need investigation. No values have been filled or excluded automatically.",
                ))
        for field in ("quantity", "unit_price"):
            number_errors = invalid_numbers.get(field, [])
            if number_errors:
                column = getattr(version.mapping, field)
                findings.append(_finding(
                    "numeric_parse", "error", f"Numbers cannot be parsed: {column}",
                    [row.row_id for row, _ in number_errors], column,
                    {"field": field, "count": len(number_errors), "examples": [
                        {"row_id": row.row_id, "raw": row.values[column], "reason": reason}
                        for row, reason in number_errors[:5]
                    ]},
                    "These rows cannot contribute an amount until quantity and unit price parse under the confirmed settings.",
                ))
        if invalid_dates:
            column = version.mapping.order_time
            findings.append(_finding(
                "date_parse", "error", "Dates do not match the confirmed format",
                [row.row_id for row in invalid_dates], column,
                {"format": version.settings.date_format, "count": len(invalid_dates), "examples": [
                    {"row_id": row.row_id, "raw": row.values[column]} for row in invalid_dates[:5]
                ]},
                "Amounts with valid quantity and price still count toward net sales, but remain outside monthly totals.",
            ))
        for row_ids in duplicates.values():
            if len(row_ids) > 1:
                findings.append(_finding(
                    "duplicate_rows", "review", "Identical source records need review", row_ids, None,
                    {"count": len(row_ids), "compared_columns": list(version.columns)},
                    "Every source column matches. Repeated records may still be legitimate; select specific rows before exclusion. An order ID alone is never a duplicate key.",
                ))
        for product, points in prices.items():
            if len(points) < 10:
                continue
            ordered = sorted(price for _, price in points)
            q1, q3 = _quartile(ordered, 1), _quartile(ordered, 3)
            iqr = q3 - q1
            lower, upper = q1 - 3 * iqr, q3 + 3 * iqr
            outliers = [(row_id, price) for row_id, price in points if price < lower or price > upper]
            if outliers:
                findings.append(_finding(
                    "price_outlier", "review", "Unusual prices within one product", [r for r, _ in outliers],
                    version.mapping.unit_price,
                    {"product_id": product, "valid_prices": len(points), "q1": _money(q1), "q3": _money(q3),
                     "iqr": _money(iqr), "lower_bound": _money(lower), "upper_bound": _money(upper),
                     "examples": [{"row_id": r, "price": _money(p)} for r, p in outliers[:5]]},
                    "Prices outside the product's three-IQR range are review candidates, not proven errors. No correction or exclusion is automatic.",
                ))

        metrics = Metrics(
            total_rows=len(version.rows), included_rows=included_count,
            excluded_rows=len(version.rows) - included_count, amount_rows=amount_count,
            invalid_amount_rows=included_count - amount_count, net_amount=_money(total),
            unassigned_amount=_money(unassigned), invalid_date_rows=invalid_date_count,
            monthly={month: _money(value) for month, value in sorted(monthly.items())}, currency=version.settings.currency,
        )
    return Analysis(metrics, findings, round(time.perf_counter() - started, 6), version.version_id)


def preview_repair(version: DatasetVersion, plan: RepairPlan) -> Preview:
    """Apply a validated plan to a new in-memory snapshot, never to its parent."""
    _validate_version(version)
    if plan.base_version_id != version.version_id:
        raise ConflictError("The preview belongs to an older version. Rebuild it from the active version.")
    if not isinstance(plan.reason, str) or not plan.reason.strip():
        raise ValidationError("A repair requires a nonempty reason.")
    if not plan.operations:
        raise ValidationError("A repair requires at least one operation.")
    # Frozen dataclasses still contain mutable lists and dictionaries. Detach the
    # submitted plan so changing a caller's selection cannot rewrite this preview.
    plan = RepairPlan.from_dict(plan.to_dict())
    rows = [DataRow(row.row_id, dict(row.values), row.excluded) for row in version.rows]
    positions = {row.row_id: index for index, row in enumerate(rows)}
    settings = version.settings
    changes: list[CellChange] = []
    for operation in plan.operations:
        if operation.kind not in {"set_cell", "exclude_rows", "normalize_whitespace", "change_settings"}:
            raise ValidationError(f"Unknown repair operation: {operation.kind}.")
        if not isinstance(operation.row_ids, list) or any(not isinstance(r, str) for r in operation.row_ids):
            raise ValidationError("Repair row IDs must be a list of strings.")
        if len(set(operation.row_ids)) != len(operation.row_ids):
            raise ValidationError("Select each row only once within an operation.")
        if any(row_id not in positions for row_id in operation.row_ids):
            raise ValidationError("Repair refers to an unknown source row.")

        if operation.kind == "change_settings":
            if operation.row_ids or operation.column is not None or operation.value is not None:
                raise ValidationError("A settings change cannot also edit rows or cells.")
            if not isinstance(operation.settings, dict) or not operation.settings:
                raise ValidationError("Choose at least one parse setting to change.")
            if set(operation.settings) - _SETTING_FIELDS or any(not isinstance(v, str) for v in operation.settings.values()):
                raise ValidationError("Unknown or invalid parse setting.")
            updated = replace(settings, **operation.settings)
            validate_contract(version.columns, version.mapping, updated)
            for key, after in operation.settings.items():
                before = getattr(settings, key)
                if before != after:
                    changes.append(CellChange("__settings__", key, before, after))
            settings = updated
            continue

        if not operation.row_ids:
            raise ValidationError("Select explicit row IDs for each cell or exclusion operation.")
        if operation.settings is not None:
            raise ValidationError("Cell and row operations cannot also change parse settings.")
        if operation.kind == "exclude_rows":
            if operation.column is not None or operation.value is not None:
                raise ValidationError("Exclusion changes only the selected rows' exclusion flags.")
            for row_id in operation.row_ids:
                index = positions[row_id]
                row = rows[index]
                if not row.excluded:
                    rows[index] = replace(row, excluded=True)
                    changes.append(CellChange(row_id, "__excluded__", "false", "true"))
            continue
        if operation.column not in version.columns:
            raise ValidationError("Repair refers to an unknown CSV column.")
        if operation.kind == "set_cell":
            if not isinstance(operation.value, str) or "\x00" in operation.value:
                raise ValidationError("Cell replacement must be text without NUL characters.")
            if len(operation.value.encode("utf-8")) > MAX_BYTES:
                raise ValidationError("A replacement cell exceeds the dataset size limit.")
        elif operation.value is not None:
            raise ValidationError("Whitespace normalization does not accept a replacement value.")
        for row_id in operation.row_ids:
            row = rows[positions[row_id]]
            before = row.values[operation.column]
            after = operation.value if operation.kind == "set_cell" else before.strip()
            if before != after:
                row.values[operation.column] = after
                changes.append(CellChange(row_id, operation.column, before, after))
    if not changes:
        raise ValidationError("The repair makes no changes. Select different cells or values.")
    # Bound retained source text too: repeated bulk replacements cannot expand a
    # small input into an arbitrarily large snapshot. Excluded rows still count.
    if sum(len(value.encode("utf-8")) for row in rows for value in row.values.values()) > MAX_BYTES:
        raise ValidationError("The repaired cell content exceeds the 20 MiB dataset limit.")
    revised = DatasetVersion(
        version_id=uuid.uuid4().hex, dataset_id=version.dataset_id, parent_version_id=version.version_id,
        columns=list(version.columns), rows=rows, mapping=version.mapping, settings=settings,
        created_at=datetime.now(UTC).isoformat(), description=plan.reason.strip(),
    )
    before, after = analyze(version), analyze(revised)
    with localcontext() as context:
        context.prec = 60
        amount_delta = _money(Decimal(after.metrics.net_amount) - Decimal(before.metrics.net_amount))
        months = sorted(set(before.metrics.monthly) | set(after.metrics.monthly))
        monthly_delta = {
            month: _money(Decimal(after.metrics.monthly.get(month, "0")) - Decimal(before.metrics.monthly.get(month, "0")))
            for month in months
        }
    fingerprint = hashlib.sha256(_canonical({"source": version.to_dict(), "plan": plan.to_dict()}).encode("utf-8")).hexdigest()
    return Preview(plan, revised, before, after, changes, amount_delta, monthly_delta, fingerprint)
