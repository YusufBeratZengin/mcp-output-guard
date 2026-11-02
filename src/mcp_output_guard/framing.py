"""Newline-delimited JSON-RPC 2.0 framing for the MCP stdio transport.

MCP over stdio exchanges one JSON-RPC message per line. The proxy must be able
to forward messages *byte for byte* when it does not modify them, so this module
deliberately keeps the raw bytes next to the decoded object.
"""

from __future__ import annotations

import json
from typing import IO, Any

__all__ = [
    "FramingError",
    "decode_message",
    "encode_message",
    "read_line",
    "write_line",
]

#: Refuse to buffer a single line larger than this. A runaway server must not
#: be able to exhaust the proxy's memory; 128 MiB is far above any sane result.
MAX_LINE_BYTES = 128 * 1024 * 1024


class FramingError(ValueError):
    """Raised when a line is not valid UTF-8 JSON."""


def encode_message(message: Any) -> bytes:
    """Serialize a JSON-RPC message to bytes (no trailing newline).

    Compact separators keep the proxy overhead small and deterministic.
    ``ensure_ascii=False`` keeps non-ASCII tool output readable.
    """
    try:
        text = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise FramingError(f"message is not JSON serializable: {exc}") from exc
    return text.encode("utf-8")


def decode_message(raw: bytes) -> Any:
    """Decode a single raw line into a JSON value."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FramingError(f"invalid UTF-8 in message: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FramingError(f"invalid JSON in message: {exc}") from exc


def read_line(stream: IO[bytes]) -> bytes | None:
    """Read one line from ``stream``.

    Returns the line without its trailing ``\\n`` (and optional ``\\r``), or
    ``None`` at end of stream. A final line without a newline is returned as-is.
    """
    raw = stream.readline()
    if not raw:
        return None
    if len(raw) > MAX_LINE_BYTES:
        raise FramingError(f"message exceeds {MAX_LINE_BYTES} bytes")
    if raw.endswith(b"\n"):
        raw = raw[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
    return raw


def write_line(stream: IO[bytes], raw: bytes) -> None:
    """Write one raw message followed by a newline and flush."""
    stream.write(raw)
    stream.write(b"\n")
    stream.flush()
