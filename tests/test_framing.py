"""Framing tests: the byte-level contract of the stdio transport."""

from __future__ import annotations

import io
import json

import pytest

from mcp_output_guard import framing


def test_encode_decode_roundtrip() -> None:
    message = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "çğıöşü😀"}]}}
    raw = framing.encode_message(message)
    assert b"\n" not in raw
    assert b'": ' not in raw  # compact separators, no pretty-printing
    assert framing.decode_message(raw) == message


def test_encode_keeps_non_ascii_readable() -> None:
    raw = framing.encode_message({"text": "şeker"})
    assert "şeker".encode() in raw


def test_decode_rejects_invalid_utf8() -> None:
    with pytest.raises(framing.FramingError, match="UTF-8"):
        framing.decode_message(b"\xff\xfe not utf8")


def test_decode_rejects_invalid_json() -> None:
    with pytest.raises(framing.FramingError, match="JSON"):
        framing.decode_message(b"{not json")


def test_read_line_strips_lf_and_crlf() -> None:
    stream = io.BytesIO(b'{"a":1}\r\n{"b":2}\n')
    assert framing.read_line(stream) == b'{"a":1}'
    assert framing.read_line(stream) == b'{"b":2}'
    assert framing.read_line(stream) is None


def test_read_line_returns_final_unterminated_line() -> None:
    assert framing.read_line(io.BytesIO(b"tail")) == b"tail"


def test_read_line_handles_large_lines() -> None:
    body = b'{"text":"' + b"x" * 2_000_000 + b'"}'
    assert framing.read_line(io.BytesIO(body + b"\n")) == body


def test_read_line_rejects_oversized_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(framing, "MAX_LINE_BYTES", 10)
    with pytest.raises(framing.FramingError, match="exceeds"):
        framing.read_line(io.BytesIO(b"x" * 20 + b"\n"))


def test_read_line_accepts_a_line_exactly_at_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(framing, "MAX_LINE_BYTES", 10)
    stream = io.BytesIO(b"x" * 10 + b"\n" + b"next\n")
    assert framing.read_line(stream) == b"x" * 10
    assert framing.read_line(stream) == b"next"


def test_read_line_accepts_crlf_at_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(framing, "MAX_LINE_BYTES", 10)
    stream = io.BytesIO(b"x" * 10 + b"\r\n" + b"next\n")
    assert framing.read_line(stream) == b"x" * 10
    assert framing.read_line(stream) == b"next"


def test_read_line_drains_the_tail_of_an_oversized_line(monkeypatch: pytest.MonkeyPatch) -> None:
    # The tail must be consumed, otherwise it would be parsed as the next
    # message and the session would fail with a bogus protocol error.
    monkeypatch.setattr(framing, "MAX_LINE_BYTES", 10)
    stream = io.BytesIO(b"x" * 5000 + b"\n" + b"after\n")
    with pytest.raises(framing.FramingError, match="dropped"):
        framing.read_line(stream)
    assert framing.read_line(stream) == b"after"


def test_write_line_appends_newline_and_flushes() -> None:
    stream = io.BytesIO()
    framing.write_line(stream, b'{"a":1}')
    assert stream.getvalue() == b'{"a":1}\n'


def test_decode_of_batch() -> None:
    payload = [{"jsonrpc": "2.0", "id": 1}, {"jsonrpc": "2.0", "id": 2}]
    assert framing.decode_message(json.dumps(payload).encode()) == payload
