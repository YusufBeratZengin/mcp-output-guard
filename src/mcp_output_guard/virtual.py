"""Synthetic tools injected into ``tools/list`` so the model can read spills.

The model never sees the proxy, only the tool list. Adding four tools there is
the whole trick: after a truncation the note says "the rest is here", and these
tools are how the model goes and gets it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .spill import SpillError, SpillStore
from .spill_sql import SpillSQLError, query_spill

__all__ = [
    "VIRTUAL_TOOL_NAMES",
    "call_virtual_tool",
    "is_virtual_tool",
    "tool_definitions",
]

READ_TOOL = "output_guard_read"
GREP_TOOL = "output_guard_grep"
SQL_TOOL = "output_guard_sql"
LIST_TOOL = "output_guard_spills"

VIRTUAL_TOOL_NAMES = (READ_TOOL, GREP_TOOL, SQL_TOOL, LIST_TOOL)

MAX_READ_CHARS = 20_000


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def tool_definitions() -> list[dict[str, Any]]:
    """The four tools appended to every successful ``tools/list`` response."""
    spill_arg = {
        "type": "string",
        "description": (
            "Spill reference copied from the TRUNCATED note: either the absolute "
            "file path or the 64-character spill id."
        ),
    }
    return [
        {
            "name": READ_TOOL,
            "description": (
                "Read a character window of a tool output that was too large for the "
                "context and was spilled to a local file by mcp-output-guard. Call it "
                "with the spill path from the TRUNCATED note. Returns text plus "
                "total_chars and an eof flag so you can page through the payload."
            ),
            "inputSchema": _schema(
                {
                    "spill": spill_arg,
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Character offset to start at (default 0).",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_READ_CHARS,
                        "description": f"Characters to return (default 4000, max {MAX_READ_CHARS}).",
                    },
                },
                ["spill"],
            ),
        },
        {
            "name": GREP_TOOL,
            "description": (
                "Search a spilled tool output with a Python regular expression and get "
                "matching lines with their line numbers, instead of reading the whole "
                "payload into the context."
            ),
            "inputSchema": _schema(
                {
                    "spill": spill_arg,
                    "pattern": {"type": "string", "description": "Python regular expression."},
                    "ignore_case": {"type": "boolean", "description": "Case-insensitive match."},
                    "max_matches": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": "Maximum matching lines to return (default 20).",
                    },
                },
                ["spill", "pattern"],
            ),
        },
        {
            "name": SQL_TOOL,
            "description": (
                "Run a read-only SQL query against a spilled JSON array or CSV file. "
                "The payload is loaded into a table named data; use it for counting, "
                "filtering and aggregating large results without reading them."
            ),
            "inputSchema": _schema(
                {
                    "spill": spill_arg,
                    "query": {
                        "type": "string",
                        "description": 'A single SELECT / WITH / EXPLAIN statement, e.g. "SELECT count(*) FROM data".',
                    },
                    "max_rows": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "description": "Maximum rows to return (default 50).",
                    },
                },
                ["spill", "query"],
            ),
        },
        {
            "name": LIST_TOOL,
            "description": (
                "List recent spills (path, size, originating tool, creation time) so you "
                "can find a payload that an earlier turn spilled to disk."
            ),
            "inputSchema": _schema(
                {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": "How many recent spills to list (default 20).",
                    }
                },
                [],
            ),
        },
    ]


def is_virtual_tool(name: Any) -> bool:
    return isinstance(name, str) and name in VIRTUAL_TOOL_NAMES


def _text_result(text: str, *, structured: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if structured is not None:
        result["structuredContent"] = structured
    return result


def _error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"output_guard error: {message}"}], "isError": True}


def _int_arg(arguments: Mapping[str, Any], key: str, default: int, *, low: int, high: int) -> int:
    raw = arguments.get(key, default)
    if isinstance(raw, bool):
        raise ValueError(f"{key} must be an integer")
    if isinstance(raw, str):
        try:
            raw = int(raw.strip())
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer, got {raw!r}") from exc
    if not isinstance(raw, int):
        raise ValueError(f"{key} must be an integer")
    return max(low, min(int(raw), high))


def _bool_arg(arguments: Mapping[str, Any], key: str) -> bool:
    raw = arguments.get(key, False)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes"}
    return bool(raw)


def _str_arg(arguments: Mapping[str, Any], key: str) -> str:
    raw = arguments.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{key} is required and must be a non-empty string")
    return raw.strip()


def call_virtual_tool(
    name: str,
    arguments: Mapping[str, Any] | None,
    store: SpillStore,
    *,
    max_read_chars: int = MAX_READ_CHARS,
) -> dict[str, Any]:
    """Execute one synthetic tool call and return an MCP ``tools/call`` result."""
    args: Mapping[str, Any] = arguments if isinstance(arguments, Mapping) else {}
    try:
        if name == READ_TOOL:
            ref = _str_arg(args, "spill")
            offset = _int_arg(args, "offset", 0, low=0, high=2**31)
            limit = _int_arg(args, "limit", 4000, low=1, high=max_read_chars)
            payload = store.read(ref, offset=offset, limit=limit)
            header = (
                f"{payload['path']}\n"
                f"chars {payload['offset']}..{payload['offset'] + payload['returned_chars']} "
                f"of {payload['total_chars']} (eof={payload['eof']})\n---\n"
            )
            return _text_result(header + payload["text"], structured=payload)

        if name == GREP_TOOL:
            ref = _str_arg(args, "spill")
            pattern = _str_arg(args, "pattern")
            max_matches = _int_arg(args, "max_matches", 20, low=1, high=200)
            payload = store.grep(
                ref, pattern, max_matches=max_matches, ignore_case=_bool_arg(args, "ignore_case")
            )
            if not payload["matches"]:
                body = f"no matches for {pattern!r} in {payload['path']}"
            else:
                body = "\n".join(f"{m['line']}: {m['text']}" for m in payload["matches"])
            header = (
                f"{payload['total_matches']} matching line(s) in {payload['path']}, "
                f"showing {payload['returned_matches']}\n---\n"
            )
            return _text_result(header + body, structured=payload)

        if name == SQL_TOOL:
            ref = _str_arg(args, "spill")
            query = _str_arg(args, "query")
            max_rows = _int_arg(args, "max_rows", 50, low=1, high=500)
            path = store.resolve(ref)
            payload = query_spill(path, query, max_rows=max_rows)
            header = (
                f"{payload['row_count']} row(s) from {payload['table_rows']} loaded "
                f"(columns: {', '.join(payload['columns']) or 'none'})\n---\n"
            )
            body = json.dumps(
                [dict(zip(payload["columns"], row, strict=False)) for row in payload["rows"]],
                ensure_ascii=False,
                indent=None,
                default=str,
            )
            return _text_result(header + body, structured=payload)

        if name == LIST_TOOL:
            limit = _int_arg(args, "limit", 20, low=1, high=100)
            records = store.list()[:limit]
            lines = [
                f"{r.get('spill_id', '')[:12]}  {r.get('size_bytes', 0):>10} B  "
                f"{r.get('tool') or '-'}  {r.get('path')}"
                for r in records
            ]
            header = f"{len(records)} spill(s) in {store.directory}\n---\n"
            return _text_result(
                header + ("\n".join(lines) if lines else "no spills yet"),
                structured={"spills": records, "count": len(records)},
            )

        return _error_result(f"unknown virtual tool: {name}")
    except (OSError, SpillError, SpillSQLError, ValueError) as exc:
        return _error_result(str(exc))
