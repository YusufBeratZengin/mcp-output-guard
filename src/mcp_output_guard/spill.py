"""On-disk store for the full (untruncated) tool output.

Every spill is a file inside one directory, named after the SHA-256 of its
contents, so identical payloads deduplicate naturally. Files are created with
mode ``0600`` and the directory with ``0700`` because tool output routinely
contains secrets (environment dumps, database rows, tokens in logs).

References handed back to the model are *contained*: :meth:`SpillStore.resolve`
refuses anything that escapes the spill directory, whether by ``..`` or by a
symlink.
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
    "DEFAULT_TTL_SECONDS",
    "SpillError",
    "SpillRecord",
    "SpillStore",
    "default_store",
]

DEFAULT_FILE_MODE = 0o600
DEFAULT_DIR_MODE = 0o700
DEFAULT_TTL_SECONDS = 7 * 24 * 3600

_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")
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
        self.directory = Path(directory).expanduser()
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
        meta_path = directory / f"{spill_id}.meta.json"
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
                p for p in self.directory.glob(f"{ref}*") if not p.name.endswith(".meta.json")
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
    ) -> dict[str, Any]:
        """Regex-search a spill file line by line."""
        if max_matches <= 0:
            raise SpillError("max_matches must be positive")
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            raise SpillError(f"invalid regular expression: {exc}") from exc
        path = self.resolve(ref)
        text = path.read_text(encoding="utf-8", errors="replace")
        matches: list[dict[str, Any]] = []
        total = 0
        for number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                total += 1
                if len(matches) < max_matches:
                    matches.append({"line": number, "text": line[:400]})
        return {
            "spill_id": path.stem,
            "path": str(path),
            "pattern": pattern,
            "total_matches": total,
            "returned_matches": len(matches),
            "matches": matches,
        }

    def list(self) -> builtins.list[dict[str, Any]]:
        """List spills, newest first."""
        if not self.directory.exists():
            return []
        records: list[dict[str, Any]] = []
        for meta_path in self.directory.glob("*.meta.json"):
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
        """Delete spill files older than the TTL. Returns the number removed."""
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl < 0 or not self.directory.exists():
            return 0
        cutoff = time.time() - ttl
        removed = 0
        for path in list(self.directory.iterdir()):
            if path.is_dir():
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                path.unlink()
                removed += 1
            except OSError:  # pragma: no cover - raced with another cleanup
                continue
        return removed

    def stats(self) -> dict[str, Any]:
        files = [p for p in self.directory.glob("*") if p.is_file() and not p.name.endswith(".meta.json")] if self.directory.exists() else []
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
