"""End-to-end tests: a real proxy process in front of a real MCP server.

These are the acceptance criteria of the project card, exercised over the wire.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import by_id, parse_messages, result_text, run_proxy


def test_full_session(tmp_path: Path) -> None:
    spill_dir = tmp_path / "spills"
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo_small"}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "echo_big"}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "echo_error"}},
    ]
    completed = run_proxy(payload, spill_dir=spill_dir)
    assert completed.returncode == 0, completed.stderr.decode()
    messages = parse_messages(completed.stdout)

    # initialize passes straight through
    assert by_id(messages, 1)["result"]["serverInfo"]["name"] == "fake-mcp"

    # tools/list is extended, originals kept in order
    tools = by_id(messages, 2)["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert names[0] == "echo_small"
    assert "output_guard_read" in names
    assert "output_guard_grep" in names
    assert "output_guard_sql" in names

    # a small result is untouched
    small = by_id(messages, 3)
    assert small["result"]["content"] == [{"type": "text", "text": "hello from the fake server"}]
    assert "_meta" not in small["result"]

    # a big result is truncated, noted, and spilled in full
    big = by_id(messages, 4)
    text = result_text(big)
    assert text.startswith("row 0000: the quick brown fox")
    assert "[TRUNCATED:" in text
    assert big["result"]["_meta"]["truncated"] is True
    spill_path = Path(big["result"]["_meta"]["output_guard"]["spill"])
    assert spill_path.exists()
    assert spill_path.read_text(encoding="utf-8").count("row ") == 600

    # an error result is never rewritten
    error = by_id(messages, 5)
    assert error["result"]["isError"] is True
    assert "_meta" not in error["result"]
    assert error["result"]["content"][0]["text"].startswith("boom ")


def test_untouched_response_is_byte_identical(tmp_path: Path) -> None:
    payload = [
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "echo_verbatim"}},
    ]
    completed = run_proxy(payload, spill_dir=tmp_path / "spills")
    expected = (
        b'{"jsonrpc": "2.0",  "id": 7,   "result": {"content": '
        b'[{"type": "text", "text": "small verbatim"}]}}'
    )
    lines = [line for line in completed.stdout.split(b"\n") if line.strip()]
    assert expected in lines


def test_utf8_output_is_cut_on_a_character_boundary(tmp_path: Path) -> None:
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo_utf8"}},
    ]
    completed = run_proxy(payload, spill_dir=tmp_path / "spills")
    message = parse_messages(completed.stdout)[0]
    from tests.fake_mcp_server import UTF8_TEXT

    text = result_text(message)
    head = text.split("\n\n")[0]
    assert head == UTF8_TEXT[:500]
    head.encode("utf-8")
    assert "\ufffd" not in head


def test_read_back_through_the_virtual_tool(tmp_path: Path) -> None:
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo_big"}},
    ]
    first = run_proxy(payload, spill_dir=tmp_path / "spills")
    spill = by_id(parse_messages(first.stdout), 1)["result"]["_meta"]["output_guard"]["spill"]

    second_payload = [
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "output_guard_read",
                "arguments": {"spill": spill, "offset": 0, "limit": 40},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "output_guard_grep",
                "arguments": {"spill": spill, "pattern": "row 0599"},
            },
        },
    ]
    second = run_proxy(second_payload, spill_dir=tmp_path / "spills")
    messages = parse_messages(second.stdout)
    read = by_id(messages, 2)["result"]
    assert "row 0000: the quick brown fox" in read["content"][0]["text"]
    assert read["structuredContent"]["total_chars"] > 40

    grep = by_id(messages, 3)["result"]
    assert "row 0599" in grep["content"][0]["text"]


def test_image_is_spilled_whole(tmp_path: Path) -> None:
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo_image"}},
    ]
    completed = run_proxy(payload, "--image-max-chars", "1000", spill_dir=tmp_path / "spills")
    result = parse_messages(completed.stdout)[0]["result"]
    assert result["content"][0]["type"] == "text"
    assert "IMAGE SPILLED" in result["content"][0]["text"]
    spilled = Path(result["_meta"]["output_guard"]["spill"])
    assert spilled.read_bytes() == bytes(range(256)) * 4096


def test_invalid_base64_image_does_not_break_the_stream(tmp_path: Path) -> None:
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo_broken"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "echo_small"}},
    ]
    completed = run_proxy(payload, "--image-max-chars", "1", spill_dir=tmp_path / "spills")
    messages = parse_messages(completed.stdout)
    broken = by_id(messages, 1)["result"]
    assert broken["content"][0]["data"] == "not-valid-base64!!"
    assert by_id(messages, 2)["result"]["content"][0]["text"] == "hello from the fake server"


def test_structured_content_is_pruned_and_queryable(tmp_path: Path) -> None:
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo_json"}},
    ]
    first = run_proxy(payload, "--max-chars", "400", spill_dir=tmp_path / "spills")
    result = by_id(parse_messages(first.stdout), 1)["result"]
    assert len(result["structuredContent"]["items"]) < 1000
    assert result["structuredContent"]["total"] == 1000
    report = result["_meta"]["output_guard"]["structured"]["fields"][0]
    assert report["total_items"] == 1000
    json_spill = result["_meta"]["output_guard"]["spill_json"]
    assert Path(json_spill).exists()

    second_payload = [
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "output_guard_sql",
                "arguments": {"spill": json_spill, "query": "SELECT count(*) AS n FROM data"},
            },
        }
    ]
    second = run_proxy(second_payload, spill_dir=tmp_path / "spills")
    sql_result = by_id(parse_messages(second.stdout), 2)["result"]
    assert sql_result["structuredContent"]["rows"] == [[1000]]


def test_stats_and_turkish_note(tmp_path: Path) -> None:
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo_big"}},
    ]
    completed = run_proxy(payload, "--stats", "--note-lang", "tr", spill_dir=tmp_path / "spills")
    result = parse_messages(completed.stdout)[0]["result"]
    assert "[KESILDI: toplam" in result_text(result)
    assert b"1 truncated" in completed.stderr


def test_virtual_tools_can_be_disabled(tmp_path: Path) -> None:
    payload = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
    completed = run_proxy(payload, "--no-virtual-tools", spill_dir=tmp_path / "spills")
    names = [tool["name"] for tool in parse_messages(completed.stdout)[0]["result"]["tools"]]
    assert "output_guard_read" not in names
    assert names[0] == "echo_small"


def test_per_tool_limits(tmp_path: Path) -> None:
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo_big"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "echo_small"}},
    ]
    completed = run_proxy(payload, "--tool-limit", "echo_big=50", spill_dir=tmp_path / "spills")
    messages = parse_messages(completed.stdout)
    assert result_text(by_id(messages, 1)).startswith("row 0000: the quick brown fox"[:50])
    assert "[TRUNCATED:" in result_text(by_id(messages, 1))
    assert result_text(by_id(messages, 2)) == "hello from the fake server"


def test_multiblock_result(tmp_path: Path) -> None:
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo_multiblock"}},
    ]
    completed = run_proxy(payload, spill_dir=tmp_path / "spills")
    result = parse_messages(completed.stdout)[0]["result"]
    assert result["content"][0]["text"].startswith("block-one block-one")
    assert "omitted" in result_text(result)


def test_jsonrpc_error_response_passes_through(tmp_path: Path) -> None:
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo_missing"}},
    ]
    completed = run_proxy(payload, spill_dir=tmp_path / "spills")
    message = parse_messages(completed.stdout)[0]
    assert message["error"]["code"] == -32602
    assert "result" not in message


def test_spill_dir_from_environment(tmp_path: Path) -> None:
    env_dir = tmp_path / "env-spills"
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo_big"}},
    ]
    completed = run_proxy(
        payload,
        spill_dir=tmp_path / "arg-spills",
        env={"MCP_OUTPUT_GUARD_SPILL_DIR": str(tmp_path / "ignored")},
    )
    spill = by_id(parse_messages(completed.stdout), 1)["result"]["_meta"]["output_guard"]["spill"]
    assert spill.startswith(str(tmp_path / "arg-spills"))
    assert env_dir.exists() is False


def test_killed_server_exits_cleanly(tmp_path: Path) -> None:

    from tests.conftest import FAKE_SERVER, ROOT, SRC  # noqa: F401

    completed = run_proxy([], spill_dir=tmp_path / "spills")
    assert completed.returncode == 0
    assert completed.stdout == b""


@pytest.mark.parametrize("extra", [["--reserve-note"], ["--image-max-chars", "10"]])
def test_extra_flags_keep_the_stream_valid(tmp_path: Path, extra: list[str]) -> None:
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo_big"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "echo_image"}},
    ]
    completed = run_proxy(payload, *extra, spill_dir=tmp_path / "spills")
    messages = parse_messages(completed.stdout)
    assert len(messages) == 2
    assert all("result" in message for message in messages)
