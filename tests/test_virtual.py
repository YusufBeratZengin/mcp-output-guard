"""Virtual tool tests: the four synthetic tools added to tools/list."""

from __future__ import annotations

import json

from mcp_output_guard import virtual
from mcp_output_guard.spill import SpillStore


def test_definitions_are_well_formed() -> None:
    definitions = virtual.tool_definitions()
    assert len(definitions) == 4
    names = [d["name"] for d in definitions]
    assert names == list(virtual.VIRTUAL_TOOL_NAMES)
    for definition in definitions:
        assert definition["description"]
        schema = definition["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert isinstance(schema["required"], list)
        assert "spill" in schema["properties"] or definition["name"] == virtual.LIST_TOOL


def test_is_virtual_tool() -> None:
    assert virtual.is_virtual_tool("output_guard_read")
    assert not virtual.is_virtual_tool("read_file")
    assert not virtual.is_virtual_tool(None)


def test_read_returns_window(store: SpillStore) -> None:
    record = store.put_text("abcdefghij")
    result = virtual.call_virtual_tool(
        virtual.READ_TOOL, {"spill": record.spill_id, "offset": 2, "limit": 3}, store
    )
    text = result["content"][0]["text"]
    assert "cde" in text
    assert result["structuredContent"]["total_chars"] == 10
    assert "eof" in text


def test_read_accepts_string_numbers(store: SpillStore) -> None:
    record = store.put_text("abcdefghij")
    result = virtual.call_virtual_tool(
        virtual.READ_TOOL, {"spill": str(record.path), "offset": "1", "limit": "2"}, store
    )
    assert result["structuredContent"]["text"] == "bc"


def test_read_missing_argument(store: SpillStore) -> None:
    result = virtual.call_virtual_tool(virtual.READ_TOOL, {}, store)
    assert result["isError"] is True
    assert "spill is required" in result["content"][0]["text"]


def test_read_unknown_spill(store: SpillStore) -> None:
    result = virtual.call_virtual_tool(virtual.READ_TOOL, {"spill": "nope.txt"}, store)
    assert result["isError"] is True
    assert "not found" in result["content"][0]["text"]


def test_read_bad_integer(store: SpillStore) -> None:
    record = store.put_text("abc")
    result = virtual.call_virtual_tool(
        virtual.READ_TOOL, {"spill": record.spill_id, "offset": "abc"}, store
    )
    assert result["isError"] is True
    assert "integer" in result["content"][0]["text"]

    result = virtual.call_virtual_tool(
        virtual.READ_TOOL, {"spill": record.spill_id, "offset": True}, store
    )
    assert result["isError"] is True


def test_read_clamps_limit(store: SpillStore) -> None:
    record = store.put_text("x" * 100)
    result = virtual.call_virtual_tool(
        virtual.READ_TOOL, {"spill": record.spill_id, "limit": 10**9}, store, max_read_chars=50
    )
    assert result["structuredContent"]["returned_chars"] == 50


def test_grep(store: SpillStore) -> None:
    record = store.put_text("alpha\nbeta\nBETA\n")
    result = virtual.call_virtual_tool(
        virtual.GREP_TOOL, {"spill": record.spill_id, "pattern": "beta", "ignore_case": "true"}, store
    )
    assert "2 matching line(s)" in result["content"][0]["text"]
    assert result["structuredContent"]["total_matches"] == 2


def test_grep_no_match(store: SpillStore) -> None:
    record = store.put_text("alpha\n")
    result = virtual.call_virtual_tool(
        virtual.GREP_TOOL, {"spill": record.spill_id, "pattern": "zzz"}, store
    )
    assert "no matches" in result["content"][0]["text"]


def test_grep_invalid_pattern(store: SpillStore) -> None:
    record = store.put_text("alpha\n")
    result = virtual.call_virtual_tool(
        virtual.GREP_TOOL, {"spill": record.spill_id, "pattern": "(["}, store
    )
    assert result["isError"] is True


def test_sql_tool(store: SpillStore) -> None:
    record = store.put_json([{"id": i, "name": f"n{i}"} for i in range(50)])
    result = virtual.call_virtual_tool(
        virtual.SQL_TOOL,
        {"spill": record.spill_id, "query": "SELECT count(*) AS n FROM data"},
        store,
    )
    assert result.get("isError") is not True
    assert result["structuredContent"]["rows"] == [[50]]
    assert "n" in result["content"][0]["text"]


def test_sql_tool_rejects_writes(store: SpillStore) -> None:
    record = store.put_json([{"id": 1}])
    result = virtual.call_virtual_tool(
        virtual.SQL_TOOL, {"spill": record.spill_id, "query": "DROP TABLE data"}, store
    )
    assert result["isError"] is True


def test_list_tool(store: SpillStore) -> None:
    store.put_text("one", tool="t1")
    store.put_text("two", tool="t2")
    result = virtual.call_virtual_tool(virtual.LIST_TOOL, {"limit": 1}, store)
    assert result["structuredContent"]["count"] == 1
    assert "spill(s) in" in result["content"][0]["text"]


def test_list_tool_empty(store: SpillStore) -> None:
    result = virtual.call_virtual_tool(virtual.LIST_TOOL, {}, store)
    assert "no spills yet" in result["content"][0]["text"]


def test_unknown_virtual_tool(store: SpillStore) -> None:
    result = virtual.call_virtual_tool("output_guard_nope", {}, store)
    assert result["isError"] is True
    assert "unknown virtual tool" in result["content"][0]["text"]


def test_arguments_not_a_mapping(store: SpillStore) -> None:
    result = virtual.call_virtual_tool(virtual.LIST_TOOL, ["not", "a", "mapping"], store)  # type: ignore[arg-type]
    assert "spill(s) in" in result["content"][0]["text"]


def test_sql_body_is_valid_json(store: SpillStore) -> None:
    record = store.put_json([{"id": 7}])
    result = virtual.call_virtual_tool(
        virtual.SQL_TOOL, {"spill": record.spill_id, "query": "SELECT * FROM data"}, store
    )
    body = result["content"][0]["text"].split("---\n", 1)[1]
    assert json.loads(body) == [{"id": 7}]
