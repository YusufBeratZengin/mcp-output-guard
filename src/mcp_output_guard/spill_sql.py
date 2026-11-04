"""Load a spilled JSON/CSV payload into SQLite and run read-only queries.

This is what turns a 50 MB tool result into something an agent can actually
interrogate: ``SELECT count(*) FROM data`` instead of 50 MB of context.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_LOADED_ROWS",
    "MAX_SQL_FILE_BYTES",
    "SpillSQLError",
    "load_rows",
    "query_spill",
]

MAX_LOADED_ROWS = 500_000
MAX_SQL_FILE_BYTES = 50 * 1024 * 1024
_ALLOWED_START = re.compile(r"^\s*(select|with|explain)\b", re.IGNORECASE)
_COMMENT_OR_SPACE = re.compile(r"^(\s|--[^\n]*\n|/\*.*?\*/)+", re.DOTALL)


class SpillSQLError(Exception):
    """Raised when a spill cannot be loaded or the query is not read-only."""


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _column_types(rows: list[list[Any]], width: int) -> list[str]:
    """Infer a SQLite affinity per column so ``WHERE id > 15`` is numeric.

    Everything from JSON arrives typed already; a column is INTEGER only when
    every non-null value in it is an int (bool excluded), REAL when they are all
    numeric, TEXT otherwise.
    """
    types: list[str] = []
    for index in range(width):
        values = [row[index] for row in rows if index < len(row) and row[index] is not None]
        if values and all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            types.append("INTEGER")
        elif values and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            types.append("REAL")
        else:
            types.append("TEXT")
    return types


def _collect_lists(value: Any, depth: int = 0) -> list[list[Any]]:
    """Every list reachable inside a JSON object, a few levels deep."""
    if depth > 3:
        return []
    if isinstance(value, list):
        return [value]
    if isinstance(value, dict):
        found: list[list[Any]] = []
        for item in value.values():
            found.extend(_collect_lists(item, depth + 1))
        return found
    return []


def _rows_from_json(value: Any) -> tuple[list[str], list[list[Any]]]:
    if isinstance(value, dict):
        candidates = [c for c in _collect_lists(value) if c]
        if not candidates:
            raise SpillSQLError("JSON object has no list to load into a table")
        # A tool result wraps the payload: prefer the biggest list of records
        # over the small "content" array of blocks next to it.
        record_lists = [c for c in candidates if all(isinstance(i, dict) for i in c)]
        value = max(record_lists or candidates, key=len)
    if not isinstance(value, list):
        raise SpillSQLError("JSON payload is not an array of records")
    if not value:
        return [], []

    if all(isinstance(item, dict) for item in value):
        columns: list[str] = []
        for item in value:
            for key in item:
                if str(key) not in columns:
                    columns.append(str(key))
        rows = []
        for item in value:
            rows.append([_flatten(item.get(col)) for col in columns])
        return columns, rows

    if all(isinstance(item, (list, tuple)) for item in value):
        width = max(len(item) for item in value)
        columns = [f"c{index}" for index in range(width)]
        return columns, [[_flatten(v) for v in item] for item in value]

    return ["value"], [[_flatten(item)] for item in value]


def _flatten(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, str)):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _rows_from_csv(path: Path) -> tuple[list[str], list[list[Any]]]:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    rows: list[list[Any]] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        try:
            header = next(reader)
        except StopIteration:
            return [], []
        columns = [str(name).strip() or f"c{index}" for index, name in enumerate(header)]
        for count, row in enumerate(reader, start=1):
            if count > MAX_LOADED_ROWS:
                raise SpillSQLError(
                    f"CSV payload exceeds {MAX_LOADED_ROWS} rows limit for in-memory SQLite"
                )
            rows.append(row)
    return columns, rows


def load_rows(path: Path) -> tuple[list[str], list[list[Any]]]:
    """Read a spill file and return ``(columns, rows)``."""
    size = path.stat().st_size
    if size > MAX_SQL_FILE_BYTES:
        raise SpillSQLError(
            f"spill file is too large for SQL loading ({size / (1024 * 1024):.1f} MB "
            f"exceeds {MAX_SQL_FILE_BYTES // (1024 * 1024)} MB limit); "
            "inspect with output_guard_read or output_guard_grep instead"
        )
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv"}:
        return _rows_from_csv(path)

    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpillSQLError(f"spill is neither JSON nor CSV: {exc}") from exc
    return _rows_from_json(value)


def query_spill(
    path: Any,
    sql: str,
    *,
    max_rows: int = 50,
) -> dict[str, Any]:
    """Run one read-only SQL statement against a spilled payload.

    The data is loaded into an in-memory SQLite database (one table named
    ``data``) and the connection is switched to ``query_only`` before the
    statement runs, so a spilled payload can never be modified in place.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise SpillSQLError("query is empty")
    if max_rows <= 0:
        raise SpillSQLError("max_rows must be positive")

    stripped = _COMMENT_OR_SPACE.sub("", sql)
    if not _ALLOWED_START.match(stripped):
        raise SpillSQLError("only SELECT / WITH / EXPLAIN queries are allowed")
    if ";" in stripped.rstrip().rstrip(";"):
        raise SpillSQLError("only a single SQL statement is allowed")

    table_path = Path(path)
    columns, rows = load_rows(table_path)
    if len(rows) > MAX_LOADED_ROWS:
        raise SpillSQLError(
            f"payload has {len(rows)} rows, above the {MAX_LOADED_ROWS} row loading limit"
        )

    conn = sqlite3.connect(":memory:")
    try:
        if not columns:
            conn.execute('CREATE TABLE "data" ("value" TEXT)')
        else:
            affinities = _column_types(rows, len(columns))
            definition = ", ".join(
                f"{_quote(name)} {kind}" for name, kind in zip(columns, affinities, strict=True)
            )
            conn.execute(f'CREATE TABLE "data" ({definition})')
            placeholders = ", ".join("?" for _ in columns)
            conn.executemany(
                f'INSERT INTO "data" VALUES ({placeholders})',
                [row[: len(columns)] + [None] * (len(columns) - len(row)) for row in rows],
            )
        conn.execute("PRAGMA query_only = ON")
        try:
            cursor = conn.execute(sql)
        except sqlite3.Error as exc:
            raise SpillSQLError(f"SQL error: {exc}") from exc
        result_columns = [d[0] for d in cursor.description] if cursor.description else []
        fetched = cursor.fetchmany(max_rows + 1)
    finally:
        conn.close()

    truncated = len(fetched) > max_rows
    visible = fetched[:max_rows]
    return {
        "path": str(table_path),
        "columns": result_columns,
        "rows": [list(row) for row in visible],
        "row_count": len(visible),
        "truncated": truncated,
        "max_rows": max_rows,
        "table_rows": len(rows),
    }
