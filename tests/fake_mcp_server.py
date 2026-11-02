"""A tiny MCP stdio server used by the end-to-end tests.

It speaks just enough JSON-RPC to exercise the proxy: initialize, tools/list and
a handful of tools/call shapes (big, small, error, multi-block, UTF-8, image,
structured JSON). The data is synthetic and deterministic on purpose.
"""

from __future__ import annotations

import base64
import json
import sys
from typing import Any

TOOLS = [
    "echo_small",
    "echo_big",
    "echo_error",
    "echo_multiblock",
    "echo_utf8",
    "echo_image",
    "echo_json",
    "echo_verbatim",
    "echo_broken",
]

BIG_TEXT = "".join(f"row {index:04d}: the quick brown fox jumps over the lazy dog\n" for index in range(600))
UTF8_TEXT = ("çğıöşü ÇĞİÖŞÜ " * 120) + "😀🚀" * 60
IMAGE_BYTES = bytes(range(256)) * 4096  # 1 MiB of deterministic bytes
IMAGE_B64 = base64.b64encode(IMAGE_BYTES).decode("ascii")
JSON_ITEMS = [{"id": index, "name": f"item-{index:04d}", "value": index * 3} for index in range(1000)]


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _result(name: str) -> dict[str, Any]:
    if name == "echo_small":
        return _text("hello from the fake server")
    if name == "echo_big":
        return _text(BIG_TEXT)
    if name == "echo_error":
        return {"content": [{"type": "text", "text": "boom " * 300}], "isError": True}
    if name == "echo_multiblock":
        return {
            "content": [
                {"type": "text", "text": "block-one " * 40},
                {"type": "text", "text": "block-two " * 40},
                {"type": "text", "text": "block-three " * 40},
            ]
        }
    if name == "echo_utf8":
        return _text(UTF8_TEXT)
    if name == "echo_image":
        return {"content": [{"type": "image", "data": IMAGE_B64, "mimeType": "image/png"}]}
    if name == "echo_json":
        return {
            "content": [{"type": "text", "text": "1000 rows returned"}],
            "structuredContent": {"items": JSON_ITEMS, "total": len(JSON_ITEMS)},
        }
    if name == "echo_broken":
        return {"content": [{"type": "image", "data": "not-valid-base64!!", "mimeType": "image/png"}]}
    return _text(f"unknown fake tool {name}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            _emit(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-mcp", "version": "0.0.0"},
                    },
                }
            )
            continue
        if method == "notifications/initialized":
            continue
        if method == "ping":
            _emit({"jsonrpc": "2.0", "id": request_id, "result": {}})
            continue
        if method == "tools/list":
            _emit(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": [
                            {"name": name, "description": f"fake tool {name}", "inputSchema": {"type": "object"}}
                            for name in TOOLS
                        ]
                    },
                }
            )
            continue
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            if name == "echo_verbatim":
                # Deliberately odd whitespace: proves unmodified responses are
                # forwarded byte for byte instead of being re-serialized.
                sys.stdout.write(
                    '{"jsonrpc": "2.0",  "id": '
                    + json.dumps(request_id)
                    + ',   "result": {"content": '
                    '[{"type": "text", "text": "small verbatim"}]}}\n'
                )
                sys.stdout.flush()
                continue
            if name == "echo_missing":
                _emit(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": "unknown tool"},
                    }
                )
                continue
            _emit({"jsonrpc": "2.0", "id": request_id, "result": _result(str(name))})
            continue
        if method == "exit":
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
