"""Small shared contracts. Monetary values cross boundaries as decimal strings."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 50_000
REQUIRED_FIELDS = ("order_id", "product_id", "quantity", "unit_price", "order_time")


class ValidationError(ValueError):
    """User input cannot be interpreted under the confirmed contract."""


class ConflictError(ValidationError):
    """The preview is stale or an idempotency key was reused differently."""


@dataclass(frozen=True)
class ColumnMap:
    order_id: str
    product_id: str
    quantity: str
    unit_price: str
    order_time: str


@dataclass(frozen=True)
class ParseSettings:
    date_format: str = "%Y-%m-%d %H:%M:%S"
    currency: str = "GBP"
    decimal_separator: str = "."
    thousands_separator: str = ""


@dataclass(frozen=True)
class DataRow:
    row_id: str
    values: dict[str, str]
    excluded: bool = False


@dataclass(frozen=True)
class DatasetVersion:
    version_id: str
    dataset_id: str
    parent_version_id: str | None
    columns: list[str]
    rows: list[DataRow]
    mapping: ColumnMap
    settings: ParseSettings
    created_at: str = ""
    description: str = "Imported original"

    def to_dict(self) -> dict[str, Any]:
        # Cells are strings, so copying their containers is enough to return a
        # detached JSON value. dataclasses.asdict recursively deep-copies every
        # scalar in 50,000-row snapshots during preview, hashing and publication.
        return {
            "version_id": self.version_id,
            "dataset_id": self.dataset_id,
            "parent_version_id": self.parent_version_id,
            "columns": list(self.columns),
            "rows": [{"row_id": row.row_id, "values": dict(row.values), "excluded": row.excluded}
                     for row in self.rows],
            "mapping": asdict(self.mapping),
            "settings": asdict(self.settings),
            "created_at": self.created_at,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DatasetVersion:
        return cls(
            **{k: v for k, v in data.items() if k not in {"rows", "mapping", "settings"}},
            rows=[DataRow(**r) for r in data["rows"]],
            mapping=ColumnMap(**data["mapping"]),
            settings=ParseSettings(**data["settings"]),
        )


@dataclass(frozen=True)
class Finding:
    finding_id: str
    rule_id: str
    severity: str  # error | review
    title: str
    row_ids: list[str]
    column: str | None
    evidence: dict[str, Any]
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Metrics:
    total_rows: int
    included_rows: int
    excluded_rows: int
    amount_rows: int
    invalid_amount_rows: int
    net_amount: str
    unassigned_amount: str
    invalid_date_rows: int
    monthly: dict[str, str]
    currency: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Analysis:
    metrics: Metrics
    findings: list[Finding]
    elapsed_seconds: float
    version_id: str = ""


@dataclass(frozen=True)
class RepairOperation:
    kind: str  # set_cell | exclude_rows | normalize_whitespace | change_settings
    row_ids: list[str] = field(default_factory=list)
    column: str | None = None
    value: str | None = None
    settings: dict[str, str] | None = None


@dataclass(frozen=True)
class RepairPlan:
    base_version_id: str
    operations: list[RepairOperation]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepairPlan:
        if not isinstance(data, dict) or set(data) != {"base_version_id", "operations", "reason"}:
            raise ValidationError("A repair plan must contain only base_version_id, operations and reason.")
        if not isinstance(data["base_version_id"], str) or not data["base_version_id"]:
            raise ValidationError("A repair plan needs a nonempty base version ID.")
        if not isinstance(data["reason"], str) or not data["reason"].strip():
            raise ValidationError("A repair plan needs a nonempty textual reason.")
        if not isinstance(data["operations"], list) or not data["operations"]:
            raise ValidationError("A repair plan needs a nonempty list of operations.")
        operations = []
        allowed = {"kind", "row_ids", "column", "value", "settings"}
        for item in data["operations"]:
            if not isinstance(item, dict) or "kind" not in item or set(item) - allowed:
                raise ValidationError("A repair operation has missing or unknown fields.")
            if not isinstance(item["kind"], str):
                raise ValidationError("Repair operation kind must be text.")
            row_ids = item.get("row_ids", [])
            if not isinstance(row_ids, list) or any(not isinstance(row_id, str) for row_id in row_ids):
                raise ValidationError("Repair row IDs must be a list of strings.")
            for name in ("column", "value"):
                if item.get(name) is not None and not isinstance(item[name], str):
                    raise ValidationError(f"Repair {name} must be text or null.")
            settings = item.get("settings")
            if settings is not None and (
                not isinstance(settings, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in settings.items())
            ):
                raise ValidationError("Repair settings must map text names to text values.")
            operations.append(RepairOperation(
                kind=item["kind"], row_ids=list(row_ids), column=item.get("column"), value=item.get("value"),
                settings=dict(settings) if settings is not None else None,
            ))
        return cls(data["base_version_id"], operations, data["reason"])


@dataclass(frozen=True)
class CellChange:
    row_id: str
    column: str
    before: str
    after: str


@dataclass(frozen=True)
class Preview:
    plan: RepairPlan
    version: DatasetVersion
    before: Analysis
    after: Analysis
    changes: list[CellChange]
    amount_delta: str
    monthly_delta: dict[str, str]
    fingerprint: str


@dataclass(frozen=True)
class Recommendation:
    finding_ids: list[str]
    explanation: str
    next_steps: list[str]
    mode: str
    elapsed_seconds: float
    fallback_reason: str | None = None
    raw_response: str | None = None
