"""On-disk store for the full (untruncated) tool output.

Every spill is a file inside one directory, named after the SHA-256 of its
contents, so identical payloads deduplicate naturally. Files are created with
mode ``0600`` and the directory with ``0700`` because tool output routinely
contains secrets (environment dumps, database rows, tokens in logs).

Those modes are POSIX permissions. On Windows ``os.chmod`` only toggles the
read-only bit and does not change the ACL: a spill file there inherits the
access control of the directory it is created in. Point ``--spill-dir`` at a
location that already has the access policy you want.

References handed back to the model are *contained*: :meth:`SpillStore.resolve`
refuses anything that escapes the spill directory, whether by ``..`` or by a
symlink, and applies the same check to id lookups and to explicit paths.
"""

from __future__ import annotations

import builtins
import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_DIR_MODE",
    "DEFAULT_FILE_MODE",
    "DEFAULT_GREP_BUDGET_SECONDS",
    "DEFAULT_GREP_SCAN_CHARS",
    "DEFAULT_TTL_SECONDS",
    "SpillError",
    "SpillRecord",
    "SpillStore",
    "default_store",
]

DEFAULT_FILE_MODE = 0o600
DEFAULT_DIR_MODE = 0o700
DEFAULT_TTL_SECONDS = 7 * 24 * 3600

#: Wall-clock budget for one ``grep`` call. A pathological expression must not
#: wedge the proxy's single input thread.
DEFAULT_GREP_BUDGET_SECONDS = 5.0

#: Characters scanned by one ``grep`` call, and the longest single line that is
#: matched against the expression (very long lines are the usual ReDoS fuel).
DEFAULT_GREP_SCAN_CHARS = 8_000_000
MAX_GREP_LINE_CHARS = 20_000
MAX_PATTERN_CHARS = 500

_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")
_META_SUFFIX = ".meta.json"
_KIND_SUFFIX = {"text": ".txt", "json": ".json", "csv": ".csv", "binary": ".bin"}


class SpillError(Exception):
    """Raised for unreadable, unknown or out-of-bounds spill references."""


@dataclass(frozen=True)
class SpillRecord:
    """Description of one spilled payload."""

    spill_id: str
    path: Path
    chars: int
    kind: str
    tool: str | None
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "spill_id": self.spill_id,
            "path": str(self.path),
            "chars": self.chars,
            "kind": self.kind,
            "tool": self.tool,
            "created_at": self.created_at,
        }


def _atomic_write(path: Path, data: bytes, mode: int = DEFAULT_FILE_MODE) -> None:
    """Write ``data`` to ``path`` atomically with restrictive permissions."""
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        os.chmod(tmp, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    with contextlib.suppress(OSError):  # pragma: no cover - exotic filesystems
        os.chmod(path, mode)


class SpillStore:
    """A directory of spilled payloads plus their metadata sidecars."""

    def __init__(self, directory: os.PathLike[str] | str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        # Absolute from the start: a relative --spill-dir must not turn
        # "spills/<hash>.txt" into "spills/spills/<hash>.txt" when the model
        # hands that reference straight back to output_guard_read.
        self.directory = Path(os.path.abspath(Path(directory).expanduser()))
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------
    def ensure(self) -> Path:
        """Create the spill directory if needed and return it."""
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=DEFAULT_DIR_MODE)
        except OSError as exc:
            raise SpillError(
                f"cannot create spill directory {self.directory}: {exc}. "
                "Pass --spill-dir with a writable location."
            ) from exc
        with contextlib.suppress(OSError):  # pragma: no cover - some network shares
            os.chmod(self.directory, DEFAULT_DIR_MODE)
        return self.directory

    # -- writing -------------------------------------------------------
    def put_text(self, text: str, *, tool: str | None = None, kind: str = "text") -> SpillRecord:
        data = text.encode("utf-8")
        return self._put(data, chars=len(text), kind=kind, tool=tool)

    def put_json(self, value: Any, *, tool: str | None = None) -> SpillRecord:
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        return self._put(text.encode("utf-8"), chars=len(text), kind="json", tool=tool)

    def put_bytes(
        self, data: bytes, *, suffix: str = ".bin", tool: str | None = None
    ) -> SpillRecord:
        safe_suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix or "") else ".bin"
        return self._put(data, chars=len(data), kind="binary", tool=tool, suffix=safe_suffix)

    def _put(
        self,
        data: bytes,
        *,
        chars: int,
        kind: str,
        tool: str | None,
        suffix: str | None = None,
    ) -> SpillRecord:
        directory = self.ensure()
        spill_id = hashlib.sha256(data).hexdigest()
        path = directory / f"{spill_id}{suffix or _KIND_SUFFIX.get(kind, '.bin')}"
        meta_path = directory / f"{spill_id}{_META_SUFFIX}"
        created = time.time()
        meta = {
            "spill_id": spill_id,
            "chars": chars,
            "kind": kind,
            "tool": tool,
            "created_at": created,
            "name": path.name,
        }
        with self._lock:
            if not path.exists():
                _atomic_write(path, data)
            _atomic_write(meta_path, json.dumps(meta).encode("utf-8"))
        return SpillRecord(
            spill_id=spill_id, path=path, chars=chars, kind=kind, tool=tool, created_at=created
        )

    # -- reading -------------------------------------------------------
    def _root(self) -> Path:
        try:
            return self.directory.resolve(strict=False)
        except OSError:  # pragma: no cover - deleted underneath us
            return self.directory

    def resolve(self, ref: str) -> Path:
        """Map a spill id or a contained path to a real file.

        Both branches - full paths and id prefixes - end in the same
        containment check, so a symlink named like a spill id cannot be used to
        read a file outside the directory.

        Raises :class:`SpillError` for empty references, missing files, and any
        path that resolves outside the spill directory.
        """
        if not isinstance(ref, str) or not ref.strip():
            raise SpillError("empty spill reference")
        ref = ref.strip().strip('"').strip("'")
        root = self._root()
        candidate: Path

        if _ID_RE.match(ref):
            matches = sorted(
                p for p in self.directory.glob(f"{ref}*") if not p.name.endswith(_META_SUFFIX)
            )
            if not matches:
                raise SpillError(f"unknown spill id: {ref}")
            if len(matches) > 1:
                raise SpillError(
                    f"ambiguous spill id {ref}: {len(matches)} files match, use more characters"
                )
            candidate = matches[0]
        else:
            given = Path(ref)
            candidate = given if given.is_absolute() else self.directory / given
            if not candidate.exists():
                raise SpillError(f"spill file not found: {ref}")

        candidate = candidate.resolve()
        try:
            inside = os.path.commonpath([str(candidate), str(root)]) == str(root)
        except ValueError:  # different drives on Windows
            inside = False
        if not inside:
            raise SpillError(f"refusing to read outside the spill directory: {ref}")

        if not candidate.is_file():
            raise SpillError(f"not a spill file: {ref}")
        return candidate

    def spill_id_for(self, path: Path) -> str:
        return path.stem

    def read(self, ref: str, *, offset: int = 0, limit: int = 4000) -> dict[str, Any]:
        """Return a character window of a spill file."""
        if limit <= 0:
            raise SpillError("limit must be positive")
        path = self.resolve(ref)
        text = path.read_text(encoding="utf-8", errors="replace")
        total = len(text)
        start = max(0, min(int(offset), total))
        end = min(total, start + int(limit))
        return {
            "spill_id": path.stem,
            "path": str(path),
            "offset": start,
            "limit": limit,
            "total_chars": total,
            "returned_chars": end - start,
            "eof": end >= total,
            "text": text[start:end],
        }

    def grep(
        self,
        ref: str,
        pattern: str,
        *,
        max_matches: int = 20,
        ignore_case: bool = False,
        budget_seconds: float = DEFAULT_GREP_BUDGET_SECONDS,
        max_scan_chars: int = DEFAULT_GREP_SCAN_CHARS,
    ) -> dict[str, Any]:
        """Regex-search a spill file line by line, under a wall-clock budget.

        Python cannot interrupt a single catastrophic ``re`` match, so the
        budget is enforced between lines and the input is bounded (at most
        ``max_scan_chars`` characters, at most :data:`MAX_GREP_LINE_CHARS` per
        line, at most :data:`MAX_PATTERN_CHARS` in the pattern). A search that
        runs out of budget fails with :class:`SpillError` instead of holding the
        proxy's input thread hostage.
        """
        if max_matches <= 0:
            raise SpillError("max_matches must be positive")
        if len(pattern) > MAX_PATTERN_CHARS:
            raise SpillError(f"regular expression is longer than {MAX_PATTERN_CHARS} characters")
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            raise SpillError(f"invalid regular expression: {exc}") from exc
        path = self.resolve(ref)
        text = path.read_text(encoding="utf-8", errors="replace")
        scan_truncated = len(text) > max_scan_chars
        if scan_truncated:
            text = text[:max_scan_chars]
        deadline = time.monotonic() + max(0.0, float(budget_seconds))
        matches: list[dict[str, Any]] = []
        total = 0
        skipped_long_lines = 0
        for number, line in enumerate(text.splitlines(), start=1):
            if number % 256 == 0 and time.monotonic() > deadline:
                raise SpillError(
                    f"search exceeded its {budget_seconds:g}s budget after {number} lines; "
                    "narrow the pattern or read a smaller window"
                )
            if len(line) > MAX_GREP_LINE_CHARS:
                line = line[:MAX_GREP_LINE_CHARS]
                skipped_long_lines += 1
            if regex.search(line):
                total += 1
                if len(matches) < max_matches:
                    matches.append({"line": number, "text": line[:400]})
        payload: dict[str, Any] = {
            "spill_id": path.stem,
            "path": str(path),
            "pattern": pattern,
            "total_matches": total,
            "returned_matches": len(matches),
            "matches": matches,
        }
        if scan_truncated:
            payload["scanned_chars"] = max_scan_chars
            payload["scan_truncated"] = True
        if skipped_long_lines:
            payload["truncated_lines"] = skipped_long_lines
        return payload

    def _verified_spill(self, meta_path: Path) -> tuple[Path, Path] | None:
        """Return ``(payload, sidecar)`` only for a sidecar this store wrote.

        The sidecar must be named after the id it contains, the id must be a
        SHA-256 hex string, and the payload it points at must be a plain file
        directly inside the spill directory (no separators, no symlink escape).
        Anything else is not ours and is never deleted.
        """
        stem = meta_path.name[: -len(_META_SUFFIX)]
        if not _ID_RE.fullmatch(stem) or len(stem) != 64:
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict) or meta.get("spill_id") != stem:
            return None
        name = meta.get("name")
        if not isinstance(name, str) or not name or Path(name).name != name:
            return None
        if name.endswith(_META_SUFFIX) or name.startswith(".tmp-"):
            return None
        target = self.directory / name
        if not target.is_file() or target.is_symlink():
            return None
        try:
            resolved = target.resolve()
            root = self._root()
            if os.path.commonpath([str(resolved), str(root)]) != str(root):
                return None
        except (OSError, ValueError):
            return None
        return target, meta_path

    def list(self) -> builtins.list[dict[str, Any]]:
        """List spills, newest first."""
        if not self.directory.exists():
            return []
        records: list[dict[str, Any]] = []
        for meta_path in self.directory.glob(f"*{_META_SUFFIX}"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            name = meta.get("name") or ""
            target = self.directory / str(name)
            meta["path"] = str(target)
            meta["size_bytes"] = target.stat().st_size if target.exists() else 0
            records.append(meta)
        records.sort(key=lambda m: float(m.get("created_at") or 0), reverse=True)
        return records

    def cleanup(self, *, ttl_seconds: int | None = None) -> int:
        """Delete *verified* spill payloads older than the TTL.

        Only pairs this store created are touched: a ``<sha256>.meta.json``
        sidecar whose id matches its name and whose ``name`` field points at a
        regular file inside the directory. Unrelated files the user keeps in the
        same folder are left alone, and so are orphan payloads without a
        valid sidecar.

        Returns the number of payloads removed.
        """
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl < 0 or not self.directory.exists():
            return 0
        cutoff = time.time() - ttl
        removed = 0
        for meta_path in sorted(self.directory.glob(f"*{_META_SUFFIX}")):
            verified = self._verified_spill(meta_path)
            if verified is None:
                continue
            target, sidecar = verified
            try:
                if target.stat().st_mtime >= cutoff or sidecar.stat().st_mtime >= cutoff:
                    continue
                target.unlink()
                sidecar.unlink()
                removed += 1
            except OSError:  # pragma: no cover - raced with another cleanup
                continue
        return removed

    def stats(self) -> dict[str, Any]:
        files = (
            [
                p
                for p in self.directory.glob("*")
                if p.is_file() and not p.name.endswith(_META_SUFFIX)
            ]
            if self.directory.exists()
            else []
        )
        return {
            "directory": str(self.directory),
            "files": len(files),
            "bytes": sum(p.stat().st_size for p in files if p.exists()),
        }

    def remove_all(self) -> None:  # pragma: no cover - used by tests/CLI clean --all
        if self.directory.exists():
            shutil.rmtree(str(self.directory))


_DEFAULT: SpillStore | None = None
_DEFAULT_LOCK = threading.Lock()


def default_dir() -> Path:
    """Default spill directory: ``$MCP_OUTPUT_GUARD_SPILL_DIR`` or ``~/.mcp-guard/spills``."""
    override = os.environ.get("MCP_OUTPUT_GUARD_SPILL_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".mcp-guard" / "spills"


def default_store() -> SpillStore:
    """Process-wide default store (lazily created)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = SpillStore(default_dir())
        return _DEFAULT
