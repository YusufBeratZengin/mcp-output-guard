"""Regressions for hard grep budgets, bounded streaming, framing and mixed batches."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from mcp_output_guard import framing, spill
from mcp_output_guard import proxy as proxy_module
from mcp_output_guard.proxy import Proxy


def mixed_proxy(tmp_path: Path) -> tuple[Proxy, io.BytesIO]:
    output = io.BytesIO()
    proxy = Proxy(["synthetic"], max_chars=100, store=spill.SpillStore(tmp_path),
                  client_out=output, log=io.BytesIO())
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "output_guard_spills"}},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ]
    forwarded = proxy.handle_client_message(framing.encode_message(batch))
    assert forwarded is not None
    assert [item["id"] for item in json.loads(forwarded)] == [2, 3]
    return proxy, output


@pytest.mark.parametrize("error", [False, True])
def test_mixed_batch_has_one_complete_response(tmp_path: Path, error: bool) -> None:
    proxy, output = mixed_proxy(tmp_path)
    assert output.getvalue() == b""
    answer = {"error": {"code": -32603, "message": "synthetic"}} if error else {"result": {}}
    response = [{"jsonrpc": "2.0", "id": i, **answer} for i in (3, 2)]
    outgoing = proxy.handle_server_message(framing.encode_message(response))
    if outgoing:
        proxy._write_client(outgoing)
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    combined = json.loads(lines[0])
    assert [item["id"] for item in combined] == [1, 2, 3]
    assert combined[1] == response[1]
    assert combined[2] == response[0]






@pytest.mark.parametrize("failure", ["eof", "read_error", "write_error"])
def test_mixed_batch_remains_complete_on_server_failure(
    tmp_path: Path, failure: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy, output = mixed_proxy(tmp_path)
    assert output.getvalue() == b""
    # A partial response must also stay buffered until completion or failure.
    assert proxy.handle_server_message(framing.encode_message(
        {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}
    )) == b""
    assert output.getvalue() == b""
    if failure in {"eof", "read_error"}:
        proxy._server_out = io.BytesIO()
        if failure == "read_error":
            monkeypatch.setattr(framing, "read_line", lambda _stream: (_ for _ in ()).throw(OSError("synthetic")))
        proxy._server_loop()
    elif failure == "write_error":
        proxy._client_in = io.BytesIO(framing.encode_message(
            {"jsonrpc": "2.0", "method": "ping", "id": 99}
        ) + b"\n")
        def refuse(raw: bytes) -> None:
            raise BrokenPipeError("synthetic")
        monkeypatch.setattr(proxy, "_write_server", refuse)
        proxy._client_loop()
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    answers = json.loads(lines[0])
    assert [item["id"] for item in answers] == [1, 2, 3]
    assert answers[1]["error"]["code"] == -32603
    assert answers[2]["result"] == {"ok": True}
    # Late replies to a failed request must not split/duplicate the batch.
    assert proxy.handle_server_message(framing.encode_message(
        {"jsonrpc": "2.0", "id": 2, "result": {}}
    )) == b""




def test_slow_batch_reply_is_not_cut_off(tmp_path: Path) -> None:
    # A slow server call is legitimate: the batch must wait for the real reply
    # and never be closed with an error by a fixed deadline.
    proxy, output = mixed_proxy(tmp_path)
    assert not hasattr(proxy_module, "_BATCH_RESPONSE_TIMEOUT_SECONDS")
    threading.Event().wait(0.2)
    assert output.getvalue() == b""
    response = [{"jsonrpc": "2.0", "id": i, "result": {"late": True}} for i in (2, 3)]
    proxy._write_client(proxy.handle_server_message(framing.encode_message(response)))
    answers = json.loads(output.getvalue().splitlines()[0])
    assert [item["id"] for item in answers] == [1, 2, 3]
    assert all("error" not in item for item in answers)


def test_grep_worker_ignores_working_directory_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A json.py in the working directory must not be imported by the worker.
    marker = tmp_path / "planted.txt"
    (tmp_path / "json.py").write_text(
        f"open({str(marker)!r}, 'w').write('ran')\n", encoding="utf-8"
    )
    store = spill.SpillStore(tmp_path / "spills")
    record = store.put_text("alpha\nbeta\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    answer = store.grep(record.spill_id, "beta")
    assert answer["total_matches"] == 1
    assert not marker.exists()


def test_expired_batch_id_can_be_reused(tmp_path: Path) -> None:
    proxy, output = mixed_proxy(tmp_path)
    proxy._fail_batches()
    assert len(output.getvalue().splitlines()) == 1
    request = framing.encode_message({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert proxy.handle_client_message(request) == request
    reply = framing.encode_message({"jsonrpc": "2.0", "id": 2, "result": {}})
    assert proxy.handle_server_message(reply) == reply




@pytest.mark.parametrize("mode", ["spawn_failure", "normal"])
def test_real_screen_miss_has_hard_deadline(tmp_path: Path, mode: str) -> None:
    # Adjacent repetitions exercise a real gap in the static screen.
    # The outer process timeout keeps this regression safe against the old code.
    code = r'''
import sys
from unittest.mock import patch
from mcp_output_guard import spill
store = spill.SpillStore(sys.argv[1])
record = store.put_text("a" * 80 + "!")
pattern = "a*" * 12 + "b"
assert spill.pattern_risk(pattern) is None
with patch.object(spill.subprocess, "Popen", side_effect=OSError("synthetic")) if sys.argv[2] == "spawn_failure" and hasattr(spill, "subprocess") else __import__("contextlib").nullcontext():
    try:
        store.grep(record.spill_id, pattern, budget_seconds=0.1)
    except spill.SpillError as exc:
        expected = "cannot start grep worker" if sys.argv[2] == "spawn_failure" else "budget"
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError("unsafe scan succeeded")
'''
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path), mode],
            capture_output=True, text=True, timeout=3,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
    except subprocess.TimeoutExpired:
        pytest.fail("grep exceeded the outer 3s deadline")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("body,tail", [(b"x" * 11, b"\nnext\n"), (b"x" * 10, b"\r")])
def test_framing_limit_plus_one_does_not_consume_next_frame(
    monkeypatch: pytest.MonkeyPatch, body: bytes, tail: bytes,
) -> None:
    monkeypatch.setattr(framing, "MAX_LINE_BYTES", 10)
    stream = io.BytesIO(body + tail)
    with pytest.raises(framing.FramingError):
        framing.read_line(stream)
    if tail.endswith(b"\n"):
        assert framing.read_line(stream) == b"next"


def test_grep_streams_bounded_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = spill.SpillStore(tmp_path)
    record = store.put_text("x" * 140_000 + "\nhit\nhit\n")
    prelude = r'''
import builtins
original_open = builtins.open
class BoundedReader:
    def __init__(self, *args, **kwargs):
        self.handle = original_open(*args, **kwargs)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.handle.close()
    def read(self, size=-1):
        assert 0 < size <= 65536, "unbounded/full-file read"
        return self.handle.read(size)
    def readline(self, size=-1):
        assert 0 < size <= 65536, "unbounded line read"
        return self.handle.readline(size)
builtins.open = BoundedReader
'''
    if hasattr(spill, "_GREP_WORKER"):
        monkeypatch.setattr(spill, "_GREP_WORKER", prelude + spill._GREP_WORKER)
    original_open = Path.open

    class BoundedReader:
        def __enter__(self):
            self.handle = original_open(record.path, encoding="utf-8")
            return self

        def __exit__(self, *args):
            self.handle.close()

        def __iter__(self):
            raise AssertionError("unbounded physical-line iteration")

        def readline(self, size=-1):
            assert 0 < size <= 65536
            return self.handle.readline(size)

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: BoundedReader())
    answer = store.grep(str(record.path), "hit")
    assert answer["total_matches"] == 2
    assert [item["line"] for item in answer["matches"]] == [2, 3]
    assert answer["truncated_lines"] == 1
