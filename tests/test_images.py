"""Binary block rules: base64 images are spilled whole, never sliced."""

from __future__ import annotations

import base64

import pytest

from mcp_output_guard.images import (
    BinaryPayloadError,
    human_size,
    is_binary_block,
    payload_chars,
    spill_binary_block,
    suffix_for_mime,
)
from mcp_output_guard.spill import SpillStore


def test_is_binary_block_variants() -> None:
    assert is_binary_block({"type": "image", "data": "AAA"})
    assert is_binary_block({"type": "audio", "data": "AAA"})
    assert is_binary_block({"type": "resource", "blob": "AAA"})
    assert not is_binary_block({"type": "image"})
    assert not is_binary_block({"type": "text", "text": "x"})
    assert not is_binary_block({"type": "resource", "uri": "file:///x"})


def test_payload_chars() -> None:
    assert payload_chars({"type": "image", "data": "abcd"}) == 4
    assert payload_chars({"type": "resource", "blob": "ab"}) == 2
    assert payload_chars({"type": "text"}) == 0


def test_suffix_for_mime() -> None:
    assert suffix_for_mime("image/png") == ".png"
    assert suffix_for_mime("image/jpeg") == ".jpg"
    assert suffix_for_mime("audio/mpeg") == ".mp3"
    assert suffix_for_mime("image/x-weird") == ".bin"


def test_suffix_for_mime_custom_subtype() -> None:
    assert suffix_for_mime("image/avif") == ".avif"
    assert suffix_for_mime(None) == ".bin"
    assert suffix_for_mime("application/octet-stream") == ".bin"


def test_suffix_for_audio_default() -> None:
    assert suffix_for_mime(None, block_type="audio") == ".wav"


def test_human_size() -> None:
    assert human_size(512) == "512 B"
    assert human_size(2048) == "2.0 KB"
    assert human_size(5 * 1024 * 1024) == "5.0 MB"


def test_spill_binary_block_roundtrip(store: SpillStore) -> None:
    raw = bytes(range(256)) * 10
    block = {"type": "image", "data": base64.b64encode(raw).decode(), "mimeType": "image/png"}
    replacement, info = spill_binary_block(block, store, tool="screenshot")
    assert replacement["type"] == "text"
    assert "IMAGE SPILLED" in replacement["text"]
    assert info["bytes"] == len(raw)
    assert info["mime_type"] == "image/png"
    from pathlib import Path

    assert Path(info["path"]).read_bytes() == raw
    assert Path(info["path"]).suffix == ".png"


def test_spill_binary_block_rejects_invalid_base64(store: SpillStore) -> None:
    with pytest.raises(BinaryPayloadError):
        spill_binary_block({"type": "image", "data": "!!!", "mimeType": "image/png"}, store)


def test_spill_binary_block_without_payload(store: SpillStore) -> None:
    with pytest.raises(BinaryPayloadError):
        spill_binary_block({"type": "image"}, store)


def test_spill_resource_block_with_nested_mime(store: SpillStore) -> None:
    block = {
        "type": "resource",
        "resource": {"mimeType": "application/pdf"},
        "blob": base64.b64encode(b"%PDF-1.4 fake").decode(),
    }
    replacement, info = spill_binary_block(block, store)
    assert info["mime_type"] == "application/pdf"
    assert "RESOURCE SPILLED" in replacement["text"]
