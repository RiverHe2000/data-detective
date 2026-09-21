from dataclasses import replace

import pytest

from data_detective.explorer import filter_rows, select_source_rows
from data_detective.models import DataRow, ValidationError


def rows():
    return [DataRow(f"source:{n}", {"order": f"ORDER-{n}", "notes": "literal [brackets]"}) for n in range(1, 601)]


def test_source_selection_reaches_later_rows_without_changing_identity():
    source = rows()
    assert select_source_rows(source, "501-503, 27, 502") == ["source:27", "source:501", "source:502", "source:503"]
    assert filter_rows(source, "order-600")[0].row_id == "source:600"
    assert len(filter_rows(source, "[brackets]")) == 600


@pytest.mark.parametrize("value", ["0", "601", "5-2", "1-999999999", "1;2", "1,", "2.5", "-1"])
def test_invalid_selection_never_partially_applies(value):
    with pytest.raises(ValidationError):
        select_source_rows(rows(), value)


def test_excluded_record_numbers_do_not_shift_and_cannot_be_silently_selected():
    source = rows()
    source[26] = replace(source[26], excluded=True)
    with pytest.raises(ValidationError, match="Already excluded"):
        select_source_rows(source, "27, 501")
    assert select_source_rows(source, "501") == ["source:501"]
    assert [row.row_id for row in filter_rows(source, status="Excluded rows")] == ["source:27"]
