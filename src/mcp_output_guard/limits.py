"""The truncation rules applied to ``tools/call`` results.

The contract, in order of importance:

1. A result that fits is returned unchanged - the very same object, so the
   proxy can forward the original bytes untouched.
2. ``isError: true`` results are never rewritten. An error message is already
   small, and mangling it hides the failure from the model.
3. Text is cut on a character boundary (never mid code point, never mid
   surrogate pair), and the note that follows tells the model exactly how much
   was hidden and where the full payload lives.
4. Binary blocks are spilled whole or left alone; they are never sliced.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .images import BinaryPayloadError, is_binary_block, payload_chars, spill_binary_block
from .spill import SpillStore, default_store

__all__ = [
    "DEFAULT_IMAGE_MAX_CHARS",
    "NOTE_TEMPLATES",
    "ToolLimits",
    "format_note",
    "is_text_block",
    "limit_result",
    "prune_structured",
    "tool_text",
]

#: A base64 image above ~1 MB of encoded characters is spilling territory:
#: that is roughly 750 KB of real pixels, far more than a model needs.
DEFAULT_IMAGE_MAX_CHARS = 1_400_000

NOTE_TEMPLATES: dict[str, str] = {
    "en": "[TRUNCATED: {total} chars total, {shown} shown, full output: {path}]",
    "tr": "[KESILDI: toplam {total} karakter, tam çıktı: {path}]",
}

_SHORT_NOTE_TEMPLATES: dict[str, str] = {
    "en": "[TRUNCATED: {total} chars total, full output: {path}]",
    "tr": "[KESILDI: toplam {total} karakter, tam çıktı: {path}]",
}


def is_text_block(block: Any) -> bool:
    return (
        isinstance(block, Mapping)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )


def tool_text(result: Mapping[str, Any]) -> str:
    """Concatenate every text block of a result, in order."""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(block["text"] for block in content if is_text_block(block))


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def _sanitize(text: str) -> str:
    """Make a string encodable as UTF-8 (a JSON payload may carry lone surrogates)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")
    return text


def _safe_cut(text: str, limit: int) -> str:
    """Cut to at most ``limit`` characters without splitting anything.

    Python slices by code point, but a JSON payload can carry a lone surrogate
    (``\\ud83d`` written without its pair). Keeping a dangling high surrogate
    would produce text that cannot be encoded back to UTF-8, so back off.
    """
    if limit <= 0:
        return ""
    if limit >= len(text):
        return text
    cut = text[:limit]
    if cut and "\ud800" <= cut[-1] <= "\udbff":
        cut = cut[:-1]
    return cut


@dataclass(frozen=True)
class ToolLimits:
    """Per-tool character budgets layered over one default."""

    default: int
    overrides: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.default <= 0:
            raise ValueError("default limit must be positive")
        for name, value in self.overrides.items():
            if value <= 0:
                raise ValueError(f"limit for tool {name!r} must be positive")

    def for_tool(self, name: str | None) -> int:
        if name is not None and name in self.overrides:
            return self.overrides[name]
        return self.default

    @classmethod
    def parse(cls, default: int, specs: Sequence[str] = ()) -> ToolLimits:
        """Parse ``--tool-limit name=N`` specs."""
        overrides: dict[str, int] = {}
        for spec in specs:
            if "=" not in spec:
                raise ValueError(f"invalid --tool-limit {spec!r}, expected NAME=CHARS")
            name, _, raw = spec.rpartition("=")
            name = name.strip()
            if not name:
                raise ValueError(f"invalid --tool-limit {spec!r}: empty tool name")
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"invalid --tool-limit {spec!r}: {raw!r} is not an integer") from exc
            overrides[name] = value
        return cls(default=default, overrides=overrides)


def format_note(
    lang: str,
    *,
    total: int,
    shown: int,
    path: str,
    short: bool = False,
) -> str:
    templates = _SHORT_NOTE_TEMPLATES if short else NOTE_TEMPLATES
    template = templates.get(lang, templates["en"])
    return template.format(total=total, shown=shown, path=path)


def _keep_count(items: Sequence[Any], budget: int, min_keep: int) -> int:
    """How many leading items fit in ``budget`` serialized characters."""
    used = 2  # the brackets
    kept = 0
    for item in items:
        size = _json_size(item) + (1 if kept else 0)
        if used + size > budget and kept >= min_keep:
            break
        used += size
        kept += 1
        if kept >= len(items):
            break
    return max(min_keep, min(kept, len(items)))


def prune_structured(
    value: Any, max_chars: int, *, min_keep: int = 1
) -> tuple[Any, dict[str, Any] | None]:
    """Keep the shape of a structured payload but only its first rows.

    A 1000-element array stays an array of the same element type; the count of
    what was dropped is reported separately (in ``_meta``) so the JSON schema a
    server advertised is not polluted with invented keys.
    """
    if _json_size(value) <= max_chars:
        return value, None

    if isinstance(value, list):
        if not value:
            return value, None
        kept = _keep_count(value, max_chars, min_keep)
        return list(value[:kept]), {
            "fields": [{"field": None, "total_items": len(value), "shown_items": kept}]
        }

    if isinstance(value, dict):
        list_fields = [(k, v) for k, v in value.items() if isinstance(v, list) and v]
        if not list_fields:
            return value, None
        list_fields.sort(key=lambda kv: _json_size(kv[1]), reverse=True)
        out = dict(value)
        reports: list[dict[str, Any]] = []
        for key, items in list_fields:
            if _json_size(out) <= max_chars:
                break
            overhead = _json_size({k: v for k, v in out.items() if k != key})
            floor = _json_size(items[:min_keep]) + 2
            budget = max(max_chars - overhead, floor)
            kept = _keep_count(items, budget, min_keep)
            if kept >= len(items):
                continue
            out[key] = items[:kept]
            reports.append({"field": key, "total_items": len(items), "shown_items": kept})
        if not reports:
            return value, None
        return out, {"fields": reports}

    return value, None


def _structured_status(
    structured: Any, pruned_info: dict[str, Any] | None, oversized: bool
) -> str:
    if structured is None:
        return "absent"
    if pruned_info is not None:
        return "pruned"
    # Oversized but shaped in a way the pruner cannot trim without inventing keys.
    return "oversized_but_unchanged" if oversized else "intact"


def limit_result(
    result: Any,
    max_chars: int,
    *,
    spill: SpillStore | None = None,
    tool: str | None = None,
    note_lang: str = "en",
    image_max_chars: int = DEFAULT_IMAGE_MAX_CHARS,
    reserve_note: bool = False,
) -> tuple[Any, dict[str, Any] | None]:
    """Apply the guard to one ``tools/call`` result.

    Returns ``(result, spill_info)``. When nothing had to change the *same*
    object comes back with ``spill_info is None``; callers rely on that to keep
    the untouched response byte-identical.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if note_lang not in NOTE_TEMPLATES:
        raise ValueError(f"unknown note language: {note_lang!r}")
    if not isinstance(result, dict):
        return result, None
    if result.get("isError") is True:
        return result, None

    content = result.get("content")
    blocks: list[Any] = list(content) if isinstance(content, list) else []
    text_total = sum(len(b["text"]) for b in blocks if is_text_block(b))

    structured = result.get("structuredContent")
    structured_oversized = structured is not None and _json_size(structured) > max_chars

    big_binary = [
        b for b in blocks if is_binary_block(b) and payload_chars(b) > image_max_chars
    ]

    if text_total <= max_chars and not structured_oversized and not big_binary:
        return result, None

    blocks = [
        {**block, "text": _sanitize(block["text"])} if is_text_block(block) else block
        for block in blocks
    ]

    store = spill if spill is not None else default_store()
    info: dict[str, Any] = {
        "truncated": True,
        "tool": tool,
        "max_chars": max_chars,
        "total_chars": text_total,
        "spilled": [],
    }

    source_result = dict(result)
    full_text = "".join(b["text"] for b in blocks if is_text_block(b))
    primary_path: str | None = None
    if full_text:
        record = store.put_text(full_text, tool=tool)
        primary_path = str(record.path)
        info["spill_id"] = record.spill_id
        info["path"] = primary_path
        info["spilled"].append({"kind": "text", "path": primary_path, "chars": len(full_text)})

    # Binary blocks: decode, store as a real file, replace with a pointer.
    new_blocks = list(blocks)
    binary_reports: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if not (is_binary_block(block) and payload_chars(block) > image_max_chars):
            continue
        try:
            replacement, binary_info = spill_binary_block(block, store, tool=tool)
        except BinaryPayloadError as exc:
            binary_reports.append({"error": str(exc), "kept_inline": True})
            continue
        new_blocks[index] = replacement
        binary_reports.append(binary_info)
        info["spilled"].append({"kind": "binary", "path": binary_info["path"], "bytes": binary_info["bytes"]})
        if primary_path is None:
            primary_path = binary_info["path"]
            info["path"] = primary_path
            info["spill_id"] = binary_info["spill_id"]
    if binary_reports:
        info["binary"] = binary_reports

    # Structured payload: keep the schema, drop the tail.
    structured_info: dict[str, Any] | None = None
    if structured_oversized:
        pruned, structured_info = prune_structured(structured, max_chars)
        if structured_info is not None:
            result = dict(result)
            result["structuredContent"] = pruned
            info["structured"] = structured_info

    # A lossless copy of the whole result, so nothing at all is lost - text,
    # binary meta or structured tail.
    json_path: str | None = None
    if structured_info is not None or binary_reports:
        json_record = store.put_json(source_result, tool=tool)
        json_path = str(json_record.path)
        info["json_path"] = json_path
        info["json_spill_id"] = json_record.spill_id

    if primary_path is None:
        primary_path = json_path or "<not persisted>"

    # Decide how much head text is shown.
    note_short = reserve_note or text_total == 0
    if reserve_note and text_total:
        note_len = len(
            format_note(note_lang, total=text_total, shown=0, path=primary_path, short=True)
        )
        head_budget = max(0, max_chars - note_len - 2)
    else:
        head_budget = max_chars

    shown_parts: list[str] = []
    remaining = head_budget
    dropped_blocks = 0
    crossed = False
    for block in new_blocks:
        if not is_text_block(block):
            continue
        text = block["text"]
        if crossed:
            dropped_blocks += 1
            continue
        if len(text) <= remaining:
            shown_parts.append(text)
            remaining -= len(text)
        else:
            cut = _safe_cut(text, remaining)
            if cut:
                shown_parts.append(cut)
            remaining = 0
            crossed = True

    shown = sum(len(p) for p in shown_parts)
    total_for_note = text_total or (
        _json_size(structured) if structured is not None else sum(payload_chars(b) for b in big_binary)
    )
    note = format_note(
        note_lang,
        total=total_for_note,
        shown=shown,
        path=primary_path,
        short=note_short,
    )
    if dropped_blocks:
        note += f" [+{dropped_blocks} text block(s) omitted]"

    # Rebuild the content array: non-text blocks stay where they were, text is
    # replaced by the head slices followed by one note block.
    rebuilt: list[Any] = []
    text_iter = iter(shown_parts)
    for block in new_blocks:
        if is_text_block(block):
            piece = next(text_iter, None)
            if piece:
                rebuilt.append({**block, "text": piece})
        else:
            rebuilt.append(block)
    rebuilt.append({"type": "text", "text": "\n\n" + note})

    new_result = dict(result)
    new_result["content"] = rebuilt

    meta = new_result.get("_meta")
    meta_dict: dict[str, Any] = dict(meta) if isinstance(meta, dict) else {}
    guard_meta: dict[str, Any] = {
        "truncated": True,
        "tool": tool,
        "max_chars": max_chars,
        "total_chars": text_total,
        "shown_chars": shown,
        "spill": primary_path,
        "structured_content": _structured_status(structured, structured_info, structured_oversized),
        "hint": (
            "The full output is on disk. Use output_guard_read(spill, offset, limit) "
            "to page through it, output_guard_grep(spill, pattern) to search it, or "
            "output_guard_sql(spill, query) if it is JSON/CSV."
        ),
    }
    if "path" in info:
        guard_meta["spill"] = info["path"]
    if "json_path" in info:
        guard_meta["spill_json"] = info["json_path"]
    if structured_info is not None:
        guard_meta["structured"] = structured_info
    if binary_reports:
        guard_meta["binary"] = [
            {k: v for k, v in item.items() if k in {"path", "bytes", "mime_type", "error"}}
            for item in binary_reports
        ]
    meta_dict["truncated"] = True
    meta_dict["output_guard"] = guard_meta
    new_result["_meta"] = meta_dict

    info["shown_chars"] = shown
    info["path"] = primary_path
    return new_result, info
