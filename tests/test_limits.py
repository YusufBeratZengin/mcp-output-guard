"""Tests for the truncation rules - the acceptance criteria of the card."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_output_guard.limits import (
    ToolLimits,
    format_note,
    is_text_block,
    limit_result,
    prune_structured,
    tool_text,
)
from mcp_output_guard.spill import SpillStore


def test_under_limit_result_is_returned_unchanged(store: SpillStore) -> None:
    result = {"content": [{"type": "text", "text": "short"}], "isError": False}
    out, info = limit_result(result, 1000, spill=store)
    assert info is None
    assert out is result


def test_exactly_at_limit_is_unchanged(store: SpillStore) -> None:
    result = {"content": [{"type": "text", "text": "x" * 50}]}
    out, info = limit_result(result, 50, spill=store)
    assert info is None and out is result


def test_error_results_are_never_truncated(store: SpillStore) -> None:
    result = {"content": [{"type": "text", "text": "traceback " * 500}], "isError": True}
    out, info = limit_result(result, 10, spill=store)
    assert info is None
    assert out is result


def test_truncation_keeps_head_note_and_full_file(store: SpillStore) -> None:
    body = "".join(f"line {i:04d}\n" for i in range(500))
    result = {"content": [{"type": "text", "text": body}], "isError": False}
    out, info = limit_result(result, 100, spill=store)

    assert info is not None and info["truncated"] is True
    text = tool_text(out)
    assert text.startswith(body[:100])
    assert "[TRUNCATED:" in text
    assert f"{len(body)} chars total" in text
    assert out["_meta"]["truncated"] is True
    assert out["_meta"]["output_guard"]["total_chars"] == len(body)
    assert out["_meta"]["output_guard"]["shown_chars"] == 100

    spill_path = Path(info["path"])
    assert spill_path.exists()
    assert spill_path.read_text(encoding="utf-8") == body


def test_original_result_is_not_mutated(store: SpillStore) -> None:
    body = "y" * 500
    result = {"content": [{"type": "text", "text": body}]}
    snapshot = json.dumps(result, sort_keys=True)
    limit_result(result, 50, spill=store)
    assert json.dumps(result, sort_keys=True) == snapshot


def test_multiple_text_blocks_share_the_budget(store: SpillStore) -> None:
    blocks = [{"type": "text", "text": f"block{i}-" + "z" * 100} for i in range(3)]
    result = {"content": blocks}
    out, info = limit_result(result, 60, spill=store)
    assert info is not None
    text = tool_text(out)
    assert text.startswith("block0-" + "z" * 53)
    assert "omitted" in text
    # every byte of every block is still in the spill file
    assert Path(info["path"]).read_text(encoding="utf-8") == "".join(b["text"] for b in blocks)


def test_text_block_before_binary_block_keeps_order(store: SpillStore) -> None:
    result = {
        "content": [
            {"type": "image", "data": "AAAA", "mimeType": "image/png"},
            {"type": "text", "text": "w" * 100},
        ]
    }
    out, info = limit_result(result, 10, spill=store)
    assert info is not None
    assert out["content"][0]["type"] == "image"
    assert out["content"][1]["text"] == "w" * 10
    assert out["content"][-1]["type"] == "text" and "TRUNCATED" in out["content"][-1]["text"]


def test_utf8_is_never_cut_mid_character(store: SpillStore) -> None:
    body = "ş" * 200
    out, info = limit_result({"content": [{"type": "text", "text": body}]}, 7, spill=store)
    assert info is not None
    head = out["content"][0]["text"]
    assert head == "ş" * 7
    assert head.encode("utf-8").decode("utf-8") == head


def test_emoji_boundary(store: SpillStore) -> None:
    body = "😀" * 100
    out, _ = limit_result({"content": [{"type": "text", "text": body}]}, 3, spill=store)
    assert out["content"][0]["text"] == "😀😀😀"


def test_lone_surrogate_never_escapes(store: SpillStore) -> None:
    body = "a" * 5 + "\ud83d" + "b" * 50
    out, _ = limit_result({"content": [{"type": "text", "text": body}]}, 6, spill=store)
    head = out["content"][0]["text"]
    # the dangling surrogate is replaced, never emitted half-written
    assert head == "aaaaa?"
    head.encode("utf-8")
    assert not any("\ud800" <= ch <= "\udbff" for ch in json.dumps(out, ensure_ascii=False))


def test_reserve_note_keeps_total_under_limit(store: SpillStore) -> None:
    body = "q" * 4000
    out, _ = limit_result(
        {"content": [{"type": "text", "text": body}]}, 400, spill=store, reserve_note=True
    )
    text = tool_text(out)
    assert len(text) <= 400
    assert text.startswith("q" * 100)
    assert "[TRUNCATED:" in text


def test_note_wins_when_it_is_longer_than_the_budget(store: SpillStore) -> None:
    # A path long enough to make the note exceed --max-chars: the model must
    # still be told what happened, so the note survives on its own.
    out, info = limit_result(
        {"content": [{"type": "text", "text": "q" * 400}]}, 20, spill=store, reserve_note=True
    )
    text = tool_text(out)
    assert text.strip().startswith("[TRUNCATED:")
    assert info is not None


def test_turkish_note(store: SpillStore) -> None:
    out, _ = limit_result(
        {"content": [{"type": "text", "text": "k" * 300}]}, 20, spill=store, note_lang="tr"
    )
    assert "[KESILDI: toplam 300 karakter, tam çıktı:" in tool_text(out)


def test_invalid_arguments(store: SpillStore) -> None:
    with pytest.raises(ValueError):
        limit_result({"content": []}, 0, spill=store)
    with pytest.raises(ValueError):
        limit_result({"content": []}, 10, spill=store, note_lang="de")


def test_non_dict_result_passes_through(store: SpillStore) -> None:
    out, info = limit_result("not a result", 10, spill=store)  # type: ignore[arg-type]
    assert out == "not a result" and info is None


def test_result_without_content_is_untouched(store: SpillStore) -> None:
    result: dict = {"isError": False}
    out, info = limit_result(result, 10, spill=store)
    assert out is result and info is None


def test_structured_content_status_when_text_is_truncated(store: SpillStore) -> None:
    # text truncated, small structured payload kept as-is: the model must be
    # told the structured part is complete
    result = {
        "content": [{"type": "text", "text": "w" * 400}],
        "structuredContent": {"ok": True},
    }
    out, _ = limit_result(result, 50, spill=store)
    assert out["_meta"]["output_guard"]["structured_content"] == "intact"


def test_structured_content_status_absent(store: SpillStore) -> None:
    out, _ = limit_result({"content": [{"type": "text", "text": "w" * 400}]}, 50, spill=store)
    assert out["_meta"]["output_guard"]["structured_content"] == "absent"


def test_structured_content_status_when_it_cannot_be_pruned(store: SpillStore) -> None:
    result = {
        "content": [{"type": "text", "text": "w" * 400}],
        "structuredContent": {"blob": "x" * 500},
    }
    out, _ = limit_result(result, 100, spill=store)
    assert out["_meta"]["output_guard"]["structured_content"] == "oversized_but_unchanged"
    assert out["structuredContent"] == {"blob": "x" * 500}


def test_text_under_limit_but_structured_over(store: SpillStore) -> None:
    items = [{"id": i, "name": f"item-{i:04d}"} for i in range(1000)]
    result = {"content": [{"type": "text", "text": "ok"}], "structuredContent": {"items": items}}
    out, info = limit_result(result, 400, spill=store)
    assert info is not None
    pruned = out["structuredContent"]["items"]
    assert 1 <= len(pruned) <= 20
    assert pruned == items[: len(pruned)]
    report = out["_meta"]["output_guard"]["structured"]["fields"][0]
    assert report["total_items"] == 1000
    assert report["shown_items"] == len(pruned)
    assert out["_meta"]["output_guard"]["structured_content"] == "pruned"
    # the full structured payload survives in the lossless JSON spill
    frozen = json.loads(Path(info["json_path"]).read_text(encoding="utf-8"))
    assert len(frozen["structuredContent"]["items"]) == 1000


def test_prune_structured_top_level_list() -> None:
    items = [{"id": i, "payload": "x" * 10} for i in range(500)]
    pruned, info = prune_structured(items, 300)
    assert isinstance(pruned, list)
    assert info is not None
    assert info["fields"][0]["total_items"] == 500
    assert pruned == items[: len(pruned)]


def test_prune_structured_keeps_schema_for_dict() -> None:
    value = {"total": 3, "rows": [{"a": 1}, {"a": 2}, {"a": 3}]}
    pruned, info = prune_structured(value, 25)
    assert info is not None
    assert "total" in pruned
    assert len(pruned["rows"]) < 3


def test_prune_structured_no_list_no_change() -> None:
    value = {"blob": "x" * 500}
    pruned, info = prune_structured(value, 10)
    assert info is None and pruned is value


def test_prune_structured_small_enough() -> None:
    value = [1, 2, 3]
    pruned, info = prune_structured(value, 100)
    assert info is None and pruned is value


def test_prune_structured_empty_list() -> None:
    pruned, info = prune_structured([], 1)
    assert info is None and pruned == []


def test_format_note_falls_back_to_english() -> None:
    assert "TRUNCATED" in format_note("xx", total=1, shown=1, path="/tmp/a")


def test_tool_limits_parse_and_lookup() -> None:
    limits = ToolLimits.parse(100, ["browser_snapshot=1000", "db.query=2000"])
    assert limits.for_tool("browser_snapshot") == 1000
    assert limits.for_tool("db.query") == 2000
    assert limits.for_tool("other") == 100
    assert limits.for_tool(None) == 100


def test_tool_limits_rejects_bad_specs() -> None:
    with pytest.raises(ValueError):
        ToolLimits.parse(100, ["oops"])
    with pytest.raises(ValueError):
        ToolLimits.parse(100, ["=5"])
    with pytest.raises(ValueError):
        ToolLimits.parse(100, ["name=abc"])
    with pytest.raises(ValueError):
        ToolLimits.parse(0)
    with pytest.raises(ValueError):
        ToolLimits(default=10, overrides={"x": 0})


def test_is_text_block_variants() -> None:
    assert is_text_block({"type": "text", "text": "a"})
    assert not is_text_block({"type": "text"})
    assert not is_text_block({"type": "image", "data": "x"})
    assert not is_text_block("nope")


def test_binary_only_result_is_spilled_not_sliced(store: SpillStore) -> None:
    import base64

    raw = bytes(range(256)) * 100
    result = {
        "content": [
            {"type": "image", "data": base64.b64encode(raw).decode(), "mimeType": "image/png"}
        ]
    }
    out, info = limit_result(result, 100, spill=store, image_max_chars=1000)
    assert info is not None
    assert out["content"][0]["type"] == "text"
    assert "IMAGE SPILLED" in out["content"][0]["text"]
    path = Path(info["path"])
    assert path.read_bytes() == raw


def test_broken_base64_image_is_left_alone(store: SpillStore) -> None:
    result = {
        "content": [
            {"type": "text", "text": "z" * 200},
            {"type": "image", "data": "%%%not-base64%%%", "mimeType": "image/png"},
        ]
    }
    out, info = limit_result(result, 20, spill=store, image_max_chars=1)
    assert info is not None
    images = [b for b in out["content"] if b.get("type") == "image"]
    assert images and images[0]["data"] == "%%%not-base64%%%"
    assert out["_meta"]["output_guard"]["binary"][0].get("error")


def test_default_store_used_when_not_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_output_guard import spill as spill_module

    monkeypatch.setattr(spill_module, "_DEFAULT", SpillStore(tmp_path / "implicit"))
    out, info = limit_result({"content": [{"type": "text", "text": "a" * 50}]}, 5)
    assert info is not None
    assert str(tmp_path / "implicit") in info["path"]
