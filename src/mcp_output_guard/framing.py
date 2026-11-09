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
    "MAX_LINE_BYTES",
    "decode_message",
    "encode_message",
    "read_line",
    "write_line",
]

#: Refuse to buffer a single line larger than this. A runaway server must not
#: be able to exhaust the proxy's memory; 128 MiB is far above any sane result.
MAX_LINE_BYTES = 128 * 1024 * 1024

#: Chunk size used to discard the remainder of an oversized line.
_DRAIN_CHUNK = 64 * 1024


class FramingError(ValueError):
    """Raised when a line is not valid UTF-8 JSON or exceeds the size limit."""


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


def _discard_rest_of_line(stream: IO[bytes]) -> None:
    """Consume bytes up to and including the next newline (or EOF).

    An oversized line must not leave its tail in the stream, where it would be
    mistaken for the next message. The discard reads in bounded chunks and
    therefore never grows memory with the line.
    """
    while True:
        try:
            chunk = stream.readline(_DRAIN_CHUNK)
        except (OSError, ValueError):  # pragma: no cover - stream died mid-line
            return
        if not chunk or chunk.endswith(b"\n"):
            return


def read_line(stream: IO[bytes]) -> bytes | None:
    """Read one line from ``stream`` without ever buffering it unbounded.

    The read is capped at :data:`MAX_LINE_BYTES`; a longer line raises
    :class:`FramingError` *after* its remainder has been consumed, so the
    caller can drop just that message and keep reading the stream.

    Returns the line without its trailing ``\\n`` (and optional ``\\r``), or
    ``None`` at end of stream. A final line without a newline is returned as-is.
    """
    raw = stream.readline(MAX_LINE_BYTES + 2)
    if not raw:
        return None
    if len(raw) > MAX_LINE_BYTES:
        # MAX bytes of content plus an LF (and optionally a CR) is still a
        # valid line; anything else is over budget and must not be parsed.
        content = raw[:-1] if raw.endswith(b"\n") else raw
        if raw.endswith(b"\r\n"):
            content = content[:-1]
        if len(content) > MAX_LINE_BYTES:
            if not raw.endswith(b"\n"):
                _discard_rest_of_line(stream)
            raise FramingError(f"message exceeds {MAX_LINE_BYTES} bytes; dropped")
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
