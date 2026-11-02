"""SQLite bridge tests: turn a spilled payload into a queryable table."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from mcp_output_guard.spill_sql import SpillSQLError, load_rows, query_spill


def _write_json(tmp_path: Path, value: object, name: str = "rows.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _write_csv(tmp_path: Path, rows: list[list[str]], name: str = "rows.csv") -> Path:
    path = tmp_path / name
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows)
    return path


def test_count_rows(tmp_path: Path) -> None:
    path = _write_json(tmp_path, [{"id": i, "name": f"n{i}"} for i in range(1000)])
    payload = query_spill(path, "SELECT count(*) FROM data")
    assert payload["rows"] == [[1000]]
    assert payload["columns"] == ["count(*)"]
    assert payload["table_rows"] == 1000


def test_filter_and_order(tmp_path: Path) -> None:
    path = _write_json(tmp_path, [{"id": i, "name": f"n{i}"} for i in range(20)])
    payload = query_spill(path, "SELECT id FROM data WHERE id > 15 ORDER BY id")
    assert [row[0] for row in payload["rows"]] == [16, 17, 18, 19]


def test_max_rows_marks_truncation(tmp_path: Path) -> None:
    path = _write_json(tmp_path, [{"id": i} for i in range(100)])
    payload = query_spill(path, "SELECT * FROM data", max_rows=5)
    assert payload["row_count"] == 5
    assert payload["truncated"] is True


def test_csv_payload(tmp_path: Path) -> None:
    path = _write_csv(tmp_path, [["id", "city"], ["1", "Ankara"], ["2", "İzmir"]])
    payload = query_spill(path, "SELECT city FROM data ORDER BY id")
    assert payload["rows"] == [["Ankara"], ["İzmir"]]


def test_tsv_payload(tmp_path: Path) -> None:
    path = tmp_path / "rows.tsv"
    path.write_text("id\tname\n1\tfake-one\n", encoding="utf-8")
    payload = query_spill(path, "SELECT name FROM data")
    assert payload["rows"] == [["fake-one"]]


def test_empty_table_still_answerable(tmp_path: Path) -> None:
    path = _write_json(tmp_path, [])
    payload = query_spill(path, "SELECT count(*) FROM data")
    assert payload["rows"] == [[0]]


def test_json_object_with_nested_list(tmp_path: Path) -> None:
    path = _write_json(tmp_path, {"meta": "x", "items": [{"a": 1}, {"a": 2}]})
    payload = query_spill(path, "SELECT count(*) FROM data")
    assert payload["rows"] == [[2]]


def test_json_object_prefers_record_list(tmp_path: Path) -> None:
    value = {"content": [{"type": "text", "text": "hi"}], "items": [{"id": 1}, {"id": 2}]}
    path = _write_json(tmp_path, value)
    payload = query_spill(path, "SELECT count(*) FROM data")
    assert payload["rows"] == [[2]]


def test_list_of_lists_and_scalars(tmp_path: Path) -> None:
    nested = _write_json(tmp_path, [[1, 2], [3, 4]], "nested.json")
    assert query_spill(nested, "SELECT c1 FROM data ORDER BY c0")["rows"] == [[2], [4]]

    scalars = _write_json(tmp_path, [10, 20, 30], "scalars.json")
    assert query_spill(scalars, "SELECT sum(value) FROM data")["rows"] == [[60]]


def test_nested_values_are_serialized(tmp_path: Path) -> None:
    path = _write_json(tmp_path, [{"id": 1, "tags": ["a", "b"]}])
    payload = query_spill(path, 'SELECT tags FROM data')
    assert payload["rows"][0][0] == '["a", "b"]'


def test_null_values(tmp_path: Path) -> None:
    path = _write_json(tmp_path, [{"id": 1, "name": None}])
    assert query_spill(path, "SELECT name FROM data")["rows"] == [[None]]


def test_write_statements_are_rejected(tmp_path: Path) -> None:
    path = _write_json(tmp_path, [{"id": 1}])
    with pytest.raises(SpillSQLError, match="SELECT"):
        query_spill(path, "DELETE FROM data")
    with pytest.raises(SpillSQLError, match="SELECT"):
        query_spill(path, "UPDATE data SET id = 2")
    with pytest.raises(SpillSQLError, match="single"):
        query_spill(path, "SELECT 1; DROP TABLE data")


def test_with_statement_is_allowed(tmp_path: Path) -> None:
    path = _write_json(tmp_path, [{"id": 1}, {"id": 2}])
    payload = query_spill(path, "WITH doubled AS (SELECT id FROM data) SELECT count(*) FROM doubled")
    assert payload["rows"] == [[2]]


def test_invalid_sql(tmp_path: Path) -> None:
    path = _write_json(tmp_path, [{"id": 1}])
    with pytest.raises(SpillSQLError, match="SQL error"):
        query_spill(path, "SELECT nope FROM data")


def test_non_tabular_payload(tmp_path: Path) -> None:
    path = tmp_path / "blob.txt"
    path.write_text("just text, not json", encoding="utf-8")
    with pytest.raises(SpillSQLError, match="neither JSON nor CSV"):
        query_spill(path, "SELECT 1")

    obj = _write_json(tmp_path, {"a": 1}, "obj.json")
    with pytest.raises(SpillSQLError, match="no list"):
        query_spill(obj, "SELECT 1")


def test_bad_arguments(tmp_path: Path) -> None:
    path = _write_json(tmp_path, [{"id": 1}])
    with pytest.raises(SpillSQLError):
        query_spill(path, "   ")
    with pytest.raises(SpillSQLError):
        query_spill(path, "SELECT 1", max_rows=0)


def test_quoted_column_names(tmp_path: Path) -> None:
    path = _write_json(tmp_path, [{"weird name": "v", 'quote"d': 1}])
    columns, rows = load_rows(path)
    assert "weird name" in columns
    payload = query_spill(path, 'SELECT "weird name" FROM data')
    assert payload["rows"] == [["v"]]


def test_comment_before_select_is_allowed(tmp_path: Path) -> None:
    path = _write_json(tmp_path, [{"id": 1}])
    payload = query_spill(path, "-- a comment\nSELECT count(*) FROM data")
    assert payload["rows"] == [[1]]
