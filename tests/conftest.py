"""Shared fixtures and helpers for the mcp-output-guard test suite."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FAKE_SERVER = Path(__file__).resolve().with_name("fake_mcp_server.py")

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mcp_output_guard.spill import SpillStore  # noqa: E402


@pytest.fixture()
def store(tmp_path: Path) -> SpillStore:
    return SpillStore(tmp_path / "spills")


@pytest.fixture()
def proxy_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env.pop("MCP_OUTPUT_GUARD_SPILL_DIR", None)
    return env


def text_result(text: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    result.update(extra)
    return result


def jsonrpc_message(method: str, request_id: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def run_proxy(
    payload: Sequence[dict[str, Any]],
    *args: str,
    spill_dir: Path | None = None,
    timeout: int = 90,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run ``mcp-output-guard run`` in front of the fake MCP server."""
    command = [
        sys.executable,
        "-m",
        "mcp_output_guard",
        "run",
        "--max-chars",
        "500",
    ]
    if spill_dir is not None:
        command += ["--spill-dir", str(spill_dir)]
    command += list(args)
    command += ["--", sys.executable, str(FAKE_SERVER)]
    body = "".join(json.dumps(message) + "\n" for message in payload)
    full_env = dict(os.environ)
    full_env["PYTHONPATH"] = str(SRC)
    if env:
        full_env.update(env)
    return subprocess.run(
        command,
        input=body.encode("utf-8"),
        capture_output=True,
        timeout=timeout,
        cwd=str(ROOT),
        env=full_env,
    )


def parse_messages(stdout: bytes) -> list[dict[str, Any]]:
    """Parse the proxy's stdout into JSON-RPC messages."""
    messages: list[dict[str, Any]] = []
    for line in stdout.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        messages.append(json.loads(line))
    return messages


def by_id(messages: Sequence[dict[str, Any]], request_id: Any) -> dict[str, Any]:
    for message in messages:
        if message.get("id") == request_id:
            return message
    raise AssertionError(f"no response with id {request_id!r} in {messages!r}")


def result_text(message: dict[str, Any]) -> str:
    """Concatenate text blocks of a JSON-RPC message *or* of a bare result."""
    container = message.get("result") if isinstance(message.get("result"), dict) else message
    blocks = container.get("content", [])
    return "".join(block.get("text", "") for block in blocks if isinstance(block, dict))
