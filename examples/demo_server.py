"""A demo MCP server that produces the kind of output that breaks agents.

Run it behind the guard::

    printf '%s\\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"read_logs"}}' \\
      | mcp-output-guard run --max-chars 400 -- python examples/demo_server.py

The log is synthetic ("fake-*" hostnames), no real data.
"""

from __future__ import annotations

import json
import sys


def read_logs() -> dict:
    lines = []
    for index in range(400):
        lines.append(
            f"2026-01-01T00:00:{index % 60:02d}Z INFO worker-{index % 4} "
            f"job=fake-job-{index:03d} host=fake-host-{index % 7} status=ok latency_ms={index % 97}"
        )
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def search_rows() -> dict:
    rows = [{"id": index, "sku": f"fake-sku-{index:04d}", "qty": index % 13} for index in range(1200)]
    return {
        "content": [{"type": "text", "text": f"{len(rows)} rows"}],
        "structuredContent": {"rows": rows, "total": len(rows)},
    }


TOOLS = {
    "read_logs": read_logs,
    "search_rows": search_rows,
}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method = message.get("method")
        if method == "initialize":
            result: dict = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "demo", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": name, "description": f"demo tool {name}", "inputSchema": {"type": "object"}}
                    for name in TOOLS
                ]
            }
        elif method == "tools/call":
            name = (message.get("params") or {}).get("name")
            handler = TOOLS.get(name)
            result = handler() if handler else {"content": [{"type": "text", "text": "unknown tool"}], "isError": True}
        else:
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message.get("id"), "result": result}) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
