"""Bounded presentation queries. Original row numbers remain stable after exclusion."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .models import DataRow, Finding, ValidationError


def source_number(row_id: str) -> int:
    return int(row_id.rsplit(":", 1)[-1])


def select_source_rows(rows: Iterable[DataRow], specification: str) -> list[str]:
    """Select explicit one-based source records, accepting e.g. '27, 31, 501-505'."""
    if not specification.strip():
        return []
    if len(specification) > 2000:
        raise ValidationError("Keep the source-row selection under 2,000 characters.")
    available = {source_number(row.row_id): row for row in rows}
    wanted: set[int] = set()
    for token in specification.split(","):
        match = re.fullmatch(r"\s*([0-9]+)(?:\s*-\s*([0-9]+))?\s*", token)
        if not match:
            raise ValidationError("Enter source row numbers separated by commas, or a range such as 501-505.")
        start = int(match[1])
        end = int(match[2] or match[1])
        if start < 1 or end < start or end > len(available):
            raise ValidationError(f"Source row numbers must be between 1 and {len(available):,}; ranges must ascend.")
        wanted.update(range(start, end + 1))
    if any(number not in available for number in wanted):
        raise ValidationError("A selected source row does not exist in this version.")
    excluded = sorted(number for number in wanted if available[number].excluded)
    if excluded:
        raise ValidationError("Already excluded source rows cannot be edited: " + ", ".join(map(str, excluded[:12])))
    return [available[number].row_id for number in sorted(wanted)]


def filter_rows(rows: Iterable[DataRow], query: str = "", status: str = "All rows") -> list[DataRow]:
    """Literal, case-insensitive search over every source field, never regex."""
    needle = query.strip().casefold()
    return [row for row in rows
            if (status == "All rows" or row.excluded == (status == "Excluded rows"))
            and (not needle or needle == str(source_number(row.row_id))
                 or any(needle in value.casefold() for value in row.values.values()))]


def filter_findings(findings: Iterable[Finding], severity: str = "All checks", query: str = "") -> list[Finding]:
    needle = query.strip().casefold()
    chosen = [finding for finding in findings
              if (severity == "All checks" or finding.severity == ("error" if severity == "Errors" else "review"))
              and (not needle or needle in " ".join([finding.title, finding.rule_id, finding.column or "",
                                                      finding.finding_id]).casefold()
                   or any(needle == str(source_number(rid)) for rid in finding.row_ids))]
    return sorted(chosen, key=lambda finding: finding.severity != "error")
