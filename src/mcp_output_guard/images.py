"""Rules for binary content blocks (images, audio, PDFs).

Base64 payloads are never sliced: half an image is useless *and* invalid, and
base64 text that is cut is no longer decodable. Instead an oversized binary
block is decoded, written to the spill directory as a real file, and replaced by
a short text block that tells the model where the bytes went.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from typing import Any

from .spill import SpillStore

__all__ = [
    "BINARY_BLOCK_TYPES",
    "BinaryPayloadError",
    "human_size",
    "is_binary_block",
    "payload_chars",
    "suffix_for_mime",
    "spill_binary_block",
]

BINARY_BLOCK_TYPES = frozenset({"image", "audio"})

_MIME_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "application/pdf": ".pdf",
}


class BinaryPayloadError(ValueError):
    """Raised when a binary block carries data that is not valid base64."""


def is_binary_block(block: Any) -> bool:
    """True for content blocks that carry base64 payloads in ``data``."""
    if not isinstance(block, Mapping):
        return False
    block_type = block.get("type")
    if block_type in BINARY_BLOCK_TYPES:
        return isinstance(block.get("data"), str)
    return block_type == "resource" and isinstance(block.get("blob"), str)


def payload_chars(block: Mapping[str, Any]) -> int:
    """Number of characters the block's base64 payload occupies in the context."""
    data = block.get("data")
    if not isinstance(data, str):
        data = block.get("blob")
    return len(data) if isinstance(data, str) else 0


def suffix_for_mime(mime: str | None, *, block_type: str = "image") -> str:
    if mime and mime in _MIME_SUFFIX:
        return _MIME_SUFFIX[mime]
    if mime and "/" in mime:
        tail = mime.rsplit("/", 1)[1].split("+")[0]
        if re.fullmatch(r"[a-z0-9]{1,6}", tail):
            return f".{tail}"
    return ".wav" if block_type == "audio" else ".bin"


def human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover - unreachable


def spill_binary_block(
    block: Mapping[str, Any],
    store: SpillStore,
    *,
    tool: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Decode ``block``, persist the bytes and return a replacement text block.

    The replacement keeps the payload out of the context while preserving the
    information the model needs: media type, real size and a readable path.
    """
    block_type = str(block.get("type", "binary"))
    mime = block.get("mimeType") if isinstance(block.get("mimeType"), str) else None
    if block_type == "resource" and isinstance(block.get("resource"), Mapping):
        inner = block["resource"]
        if isinstance(inner.get("mimeType"), str):
            mime = inner["mimeType"]
    raw_data = block.get("data")
    if not isinstance(raw_data, str):
        raw_data = block.get("blob")
    if not isinstance(raw_data, str):
        raise BinaryPayloadError("binary block has no base64 payload")

    encoded = raw_data.strip()
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BinaryPayloadError(f"invalid base64 payload: {exc}") from exc

    record = store.put_bytes(raw, suffix=suffix_for_mime(mime, block_type=block_type), tool=tool)
    info = {
        "spilled": True,
        "type": block_type,
        "mime_type": mime or "application/octet-stream",
        "path": str(record.path),
        "spill_id": record.spill_id,
        "bytes": len(raw),
        "base64_chars": len(raw_data),
        "human_size": human_size(len(raw)),
    }
    label = "IMAGE" if block_type == "image" else block_type.upper()
    text = (
        f"[{label} SPILLED: {human_size(len(raw))} of {mime or 'unknown type'} saved to "
        f"{record.path} (base64 payload was {len(raw_data)} chars, too large for the context). "
        f"Read it from disk; the bytes are intact.]"
    )
    return {"type": "text", "text": text}, info
