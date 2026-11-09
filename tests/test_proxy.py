"""Unit tests for the proxy's message handling (no subprocesses)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from mcp_output_guard import framing
from mcp_output_guard import proxy as proxy_module
from mcp_output_guard.proxy import Proxy, resolve_command
from mcp_output_guard.spill import SpillStore


def make_proxy(tmp_path: Path, **kwargs: Any) -> tuple[Proxy, io.BytesIO]:
    out = io.BytesIO()
    options: dict[str, Any] = {
        "max_chars": 100,
        "store": SpillStore(tmp_path / "spills"),
        "client_out": out,
        "log": io.BytesIO(),
    }
    options.update(kwargs)
    return Proxy(["fake-server"], **options), out


def call_message(request_id: Any, name: str = "big_tool") -> bytes:
    return framing.encode_message(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name}}
    )


def response_message(request_id: Any, result: dict[str, Any]) -> bytes:
    return framing.encode_message({"jsonrpc": "2.0", "id": request_id, "result": result})


def test_empty_command_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Proxy([], max_chars=10, store=SpillStore(tmp_path))


def test_untouched_response_is_forwarded_byte_for_byte(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path)
    proxy.handle_client_message(call_message(1))
    raw = b'{"jsonrpc":"2.0", "id": 1,   "result":{"content":[{"type":"text","text":"ok"}]}}'
    assert proxy.handle_server_message(raw) is raw


def test_oversized_response_is_rewritten(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path)
    proxy.handle_client_message(call_message(2, "db_query"))
    raw = response_message(2, {"content": [{"type": "text", "text": "z" * 500}]})
    out = proxy.handle_server_message(raw)
    assert out is not raw
    message = json.loads(out)
    assert message["id"] == 2
    assert message["result"]["_meta"]["truncated"] is True
    assert message["result"]["_meta"]["output_guard"]["tool"] == "db_query"
    assert proxy.stats.results_truncated == 1


def test_guard_failure_fails_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proxy, _ = make_proxy(tmp_path)
    proxy.handle_client_message(call_message(6, "db_query"))
    raw = response_message(6, {"content": [{"type": "text", "text": "z" * 500}]})

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("No space left on device")

    monkeypatch.setattr("mcp_output_guard.proxy.limit_result", boom)
    assert proxy.handle_server_message(raw) is raw
    assert proxy.stats.failures == 1
    assert b"forwarding it unguarded" in proxy._log.getvalue()


def test_error_result_is_not_rewritten(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path)
    proxy.handle_client_message(call_message(3))
    raw = response_message(3, {"content": [{"type": "text", "text": "x" * 500}], "isError": True})
    assert proxy.handle_server_message(raw) is raw


def test_jsonrpc_error_is_not_rewritten(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path)
    proxy.handle_client_message(call_message(4))
    raw = b'{"jsonrpc":"2.0","id":4,"error":{"code":-32603,"message":"boom"}}'
    assert proxy.handle_server_message(raw) is raw


def test_untracked_response_is_not_rewritten(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path)
    raw = response_message(99, {"content": [{"type": "text", "text": "x" * 500}]})
    assert proxy.handle_server_message(raw) is raw


def test_notification_response_is_not_rewritten(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path)
    raw = b'{"jsonrpc":"2.0","method":"notifications/progress","params":{}}'
    assert proxy.handle_server_message(raw) is raw


def test_garbage_is_forwarded(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path)
    assert proxy.handle_client_message(b"not json") == b"not json"
    assert proxy.handle_server_message(b"not json") == b"not json"
    assert proxy.handle_client_message(b"[1, 2]") == b"[1, 2]"
    assert proxy.handle_server_message(b"42") == b"42"


def test_per_tool_limit_is_applied(tmp_path: Path) -> None:
    from mcp_output_guard.limits import ToolLimits

    proxy, _ = make_proxy(tmp_path, tool_limits=ToolLimits(1000, {"narrow": 5}))
    proxy.handle_client_message(call_message(5, "narrow"))
    raw = response_message(5, {"content": [{"type": "text", "text": "abcdefghij"}]})
    message = json.loads(proxy.handle_server_message(raw))
    assert message["result"]["content"][0]["text"] == "abcde"


def test_tools_list_gets_virtual_tools(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path)
    proxy.handle_client_message(
        framing.encode_message({"jsonrpc": "2.0", "id": 10, "method": "tools/list"})
    )
    raw = response_message(10, {"tools": [{"name": "original", "inputSchema": {}}]})
    message = json.loads(proxy.handle_server_message(raw))
    names = [tool["name"] for tool in message["result"]["tools"]]
    assert names[0] == "original"
    assert names[1:] == [
        "output_guard_read",
        "output_guard_grep",
        "output_guard_sql",
        "output_guard_spills",
    ]


def test_tools_list_injection_is_idempotent(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path)
    for request_id in (11, 12):
        proxy.handle_client_message(
            framing.encode_message({"jsonrpc": "2.0", "id": request_id, "method": "tools/list"})
        )
        raw = response_message(
            request_id,
            {"tools": [{"name": "output_guard_read", "inputSchema": {}}]},
        )
        message = json.loads(proxy.handle_server_message(raw))
        names = [tool["name"] for tool in message["result"]["tools"]]
        assert names.count("output_guard_read") == 1


def test_tools_list_first_page_gets_tools_even_when_paginated(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path)
    proxy.handle_client_message(
        framing.encode_message({"jsonrpc": "2.0", "id": 13, "method": "tools/list"})
    )
    raw = response_message(13, {"tools": [{"name": "a"}], "nextCursor": "page2"})
    names = [tool["name"] for tool in json.loads(proxy.handle_server_message(raw))["result"]["tools"]]
    assert "output_guard_read" in names


def test_tools_list_second_page_is_left_alone(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path)
    proxy.handle_client_message(
        framing.encode_message(
            {"jsonrpc": "2.0", "id": 15, "method": "tools/list", "params": {"cursor": "page2"}}
        )
    )
    raw = response_message(15, {"tools": [{"name": "b"}]})
    assert proxy.handle_server_message(raw) is raw


def test_tools_list_without_tools_key(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path)
    proxy.handle_client_message(
        framing.encode_message({"jsonrpc": "2.0", "id": 14, "method": "tools/list"})
    )
    raw = response_message(14, {})
    assert proxy.handle_server_message(raw) is raw


def test_output_schema_from_tools_list_protects_structured_lists(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path, max_chars=200)
    proxy.handle_client_message(
        framing.encode_message({"jsonrpc": "2.0", "id": 40, "method": "tools/list"})
    )
    listing = response_message(
        40,
        {
            "tools": [
                {
                    "name": "pair_tool",
                    "inputSchema": {},
                    "outputSchema": {
                        "type": "object",
                        "properties": {
                            "pair": {"type": "array", "prefixItems": [{}, {}]},
                        },
                    },
                }
            ]
        },
    )
    proxy.handle_server_message(listing)

    proxy.handle_client_message(call_message(41, "pair_tool"))
    raw = response_message(
        41,
        {
            "content": [{"type": "text", "text": "ok"}],
            "structuredContent": {"pair": [{"x": "a" * 100}, {"x": "b" * 100}, {"x": "c" * 100}]},
        },
    )
    message = json.loads(proxy.handle_server_message(raw))
    result = message["result"]
    guard = result["_meta"]["output_guard"]
    # The schema pins the tuple's length, so nothing can be trimmed without
    # breaking it: the structured field leaves the reply and the whole result is
    # spilled losslessly as JSON, named by the note the model sees.
    assert "structuredContent" not in result
    assert guard["structured_content"] == "spilled"
    assert guard["structured"]["dropped"] is True
    frozen = json.loads(Path(guard["spill_json"]).read_text(encoding="utf-8"))
    assert frozen["structuredContent"]["pair"][2]["x"] == "c" * 100
    assert guard["spill_json"] in result["content"][-1]["text"]


def test_output_schema_min_items_is_enforced_end_to_end(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path, max_chars=200)
    proxy.handle_client_message(
        framing.encode_message({"jsonrpc": "2.0", "id": 42, "method": "tools/list"})
    )
    proxy.handle_server_message(
        response_message(
            42,
            {
                "tools": [
                    {
                        "name": "db_query",
                        "inputSchema": {},
                        "outputSchema": {
                            "type": "object",
                            "properties": {"rows": {"type": "array", "minItems": 5}},
                        },
                    }
                ]
            },
        )
    )
    proxy.handle_client_message(call_message(43, "db_query"))
    rows = [{"id": i, "pad": "z" * 20} for i in range(100)]
    raw = response_message(
        43,
        {"content": [{"type": "text", "text": "ok"}], "structuredContent": {"rows": rows}},
    )
    message = json.loads(proxy.handle_server_message(raw))
    assert len(message["result"]["structuredContent"]["rows"]) == 5


def test_virtual_tool_call_is_answered_locally(tmp_path: Path) -> None:
    proxy, out = make_proxy(tmp_path)
    record = proxy.store.put_text("0123456789")
    raw = framing.encode_message(
        {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": "output_guard_read",
                "arguments": {"spill": record.spill_id, "offset": 0, "limit": 3},
            },
        }
    )
    assert proxy.handle_client_message(raw) is None
    message = json.loads(out.getvalue().splitlines()[0])
    assert message["id"] == 20
    assert "012" in message["result"]["content"][0]["text"]


def test_virtual_tools_can_be_disabled(tmp_path: Path) -> None:
    proxy, out = make_proxy(tmp_path, virtual_tools=False)
    raw = framing.encode_message(
        {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {"name": "output_guard_read", "arguments": {}},
        }
    )
    assert proxy.handle_client_message(raw) == raw
    assert out.getvalue() == b""


def test_virtual_call_without_id_is_forwarded(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path)
    raw = framing.encode_message(
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "output_guard_read"}}
    )
    assert proxy.handle_client_message(raw) == raw


def test_id_types_do_not_collide(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path)
    proxy.handle_client_message(call_message(1, "small"))
    proxy.handle_client_message(call_message("1", "big"))
    big = response_message("1", {"content": [{"type": "text", "text": "y" * 500}]})
    assert proxy.handle_server_message(big) is not big
    small = response_message(1, {"content": [{"type": "text", "text": "ok"}]})
    assert proxy.handle_server_message(small) is small


def test_batch_requests_are_tracked(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path)
    batch = [
        {"jsonrpc": "2.0", "id": 30, "method": "tools/call", "params": {"name": "big"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    raw = framing.encode_message(batch)
    assert proxy.handle_client_message(raw) == raw

    responses = [
        {"jsonrpc": "2.0", "id": 30, "result": {"content": [{"type": "text", "text": "q" * 500}]}},
    ]
    merged = json.loads(proxy.handle_server_message(framing.encode_message(responses)))
    assert merged[0]["result"]["_meta"]["truncated"] is True


def test_untouched_batch_is_forwarded_byte_for_byte(tmp_path: Path) -> None:
    proxy, _ = make_proxy(tmp_path)
    batch = [{"jsonrpc": "2.0", "id": 31, "method": "ping"}]
    raw = framing.encode_message(batch)
    proxy.handle_client_message(raw)
    responses = framing.encode_message([{"jsonrpc": "2.0", "id": 31, "result": {}}])
    assert proxy.handle_server_message(responses) is responses


def test_virtual_tool_inside_a_batch_is_answered_locally(tmp_path: Path) -> None:
    proxy, out = make_proxy(tmp_path)
    batch = [
        {
            "jsonrpc": "2.0",
            "id": 40,
            "method": "tools/call",
            "params": {"name": "output_guard_spills", "arguments": {}},
        },
        {"jsonrpc": "2.0", "id": 41, "method": "tools/call", "params": {"name": "big"}},
    ]
    forwarded = proxy.handle_client_message(framing.encode_message(batch))
    assert forwarded is not None
    # The server only ever sees the call it actually implements.
    assert [item["id"] for item in json.loads(forwarded)] == [41]

    assert out.getvalue() == b""
    response = response_message(41, {"content": [{"type": "text", "text": "x" * 500}]})
    answered = json.loads(proxy.handle_server_message(response))
    assert [item["id"] for item in answered] == [40, 41]
    assert answered[1]["result"]["_meta"]["truncated"] is True
    assert answered[0]["id"] == 40
    assert answered[0]["result"]["structuredContent"]["spills"] == []
    assert proxy.stats.calls_seen == 2


def test_batch_of_only_virtual_tools_never_reaches_the_server(tmp_path: Path) -> None:
    proxy, out = make_proxy(tmp_path)
    batch = [
        {
            "jsonrpc": "2.0",
            "id": "a",
            "method": "tools/call",
            "params": {"name": "output_guard_spills", "arguments": {"limit": 5}},
        }
    ]
    assert proxy.handle_client_message(framing.encode_message(batch)) is None
    answered = json.loads(out.getvalue().decode("utf-8").strip())
    assert [item["id"] for item in answered] == ["a"]


def test_resolve_command_on_posix() -> None:
    assert resolve_command(["python", "-V"], platform_name="posix") == ["python", "-V"]


def test_resolve_command_on_windows_wraps_cmd_shims(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy_module.shutil, "which", lambda _name: r"C:\npm\npx.cmd")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\system32\cmd.exe")
    resolved = resolve_command(["npx", "-y", "server"], platform_name="nt")
    assert resolved[0].endswith("cmd.exe")
    assert resolved[1] == "/c"
    assert resolved[2].endswith("npx.cmd")
    assert resolved[3:] == ["-y", "server"]


def test_resolve_command_on_windows_plain_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy_module.shutil, "which", lambda _name: r"C:\Python\python.exe")
    assert resolve_command(["python", "s.py"], platform_name="nt")[0].endswith("python.exe")


def test_resolve_command_on_windows_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy_module.shutil, "which", lambda _name: None)
    assert resolve_command(["missing-server"], platform_name="nt") == ["missing-server"]


def test_run_reports_missing_server(tmp_path: Path) -> None:
    log = io.BytesIO()
    proxy = Proxy(
        ["definitely-not-a-real-binary-xyz"],
        max_chars=10,
        store=SpillStore(tmp_path / "spills"),
        client_in=io.BytesIO(b""),
        client_out=io.BytesIO(),
        log=log,
    )
    assert proxy.run() == 127
    assert b"cannot start server" in log.getvalue()


def test_run_with_real_child(tmp_path: Path, proxy_env: dict[str, str]) -> None:
    import sys

    payload = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
    out = io.BytesIO()
    log = io.BytesIO()
    server = (
        "import json,sys\n"
        "for line in sys.stdin:\n"
        "    msg=json.loads(line)\n"
        "    sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{}})+'\\n')\n"
        "    sys.stdout.flush()\n"
    )
    proxy = Proxy(
        [sys.executable, "-c", server],
        max_chars=10,
        store=SpillStore(tmp_path / "spills"),
        client_in=io.BytesIO(payload),
        client_out=out,
        log=log,
        platform_name="posix",
    )
    assert proxy.run() == 0
    assert b'"result"' in out.getvalue()


def test_store_directory_is_created_on_run(tmp_path: Path) -> None:
    import sys

    store = SpillStore(tmp_path / "deep" / "nested")
    proxy = Proxy(
        [sys.executable, "-c", "pass"],
        max_chars=10,
        store=store,
        client_in=io.BytesIO(b""),
        client_out=io.BytesIO(),
        log=io.BytesIO(),
        platform_name="posix",
    )
    assert proxy.run() == 0
    assert store.directory.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX exit codes")
def test_exit_code_is_propagated(tmp_path: Path) -> None:
    import sys

    proxy = Proxy(
        [sys.executable, "-c", "raise SystemExit(3)"],
        max_chars=10,
        store=SpillStore(tmp_path / "spills"),
        client_in=io.BytesIO(b""),
        client_out=io.BytesIO(),
        log=io.BytesIO(),
        platform_name="posix",
    )
    assert proxy.run() == 3
