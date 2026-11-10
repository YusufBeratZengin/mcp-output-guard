"""Spill store tests: permissions, dedupe, containment and paging."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from mcp_output_guard.spill import (
    DEFAULT_TTL_SECONDS,
    MAX_GREP_FILE_BYTES,
    MAX_READ_FILE_BYTES,
    SpillError,
    SpillStore,
    default_dir,
    default_store,
)


def test_put_text_writes_file_with_restrictive_mode(store: SpillStore, tmp_path: Path) -> None:
    record = store.put_text("hello world", tool="demo")
    assert record.path.read_text(encoding="utf-8") == "hello world"
    assert record.chars == 11
    if os.name != "nt":
        assert stat.S_IMODE(record.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    else:
        # On Windows, os.chmod only toggles read-only bits and cannot manage NTFS ACLs;
        # files inherit directory ACLs and remain readable/writable for the current user.
        assert os.access(record.path, os.R_OK | os.W_OK)
        assert os.access(store.directory, os.R_OK | os.W_OK | os.X_OK)


def test_identical_payloads_deduplicate(store: SpillStore) -> None:
    first = store.put_text("same")
    second = store.put_text("same")
    assert first.spill_id == second.spill_id
    assert first.path == second.path


def test_put_json_roundtrip(store: SpillStore) -> None:
    record = store.put_json({"items": [1, 2, 3], "ü": "ş"})
    assert record.kind == "json"
    assert record.path.suffix == ".json"
    assert "\u015f" in record.path.read_text(encoding="utf-8")


def test_put_bytes_uses_suffix_and_sanitizes_it(store: SpillStore) -> None:
    assert store.put_bytes(b"\x00\x01", suffix=".png").path.suffix == ".png"
    assert store.put_bytes(b"\x02", suffix="../../etc/passwd").path.suffix == ".bin"


def test_read_window_and_eof(store: SpillStore) -> None:
    text = "0123456789"
    record = store.put_text(text)
    first = store.read(record.spill_id, offset=0, limit=4)
    assert first["text"] == "0123"
    assert first["total_chars"] == 10
    assert first["eof"] is False
    last = store.read(str(record.path), offset=8, limit=100)
    assert last["text"] == "89"
    assert last["eof"] is True
    assert last["returned_chars"] == 2


def test_read_clamps_offset(store: SpillStore) -> None:
    record = store.put_text("abc")
    assert store.read(record.spill_id, offset=-5, limit=2)["offset"] == 0
    beyond = store.read(record.spill_id, offset=99, limit=2)
    assert beyond["text"] == ""
    assert beyond["eof"] is True


def test_read_rejects_bad_limit(store: SpillStore) -> None:
    record = store.put_text("abc")
    with pytest.raises(SpillError):
        store.read(record.spill_id, limit=0)


def test_grep_finds_lines_and_ignores_case(store: SpillStore) -> None:
    record = store.put_text("alpha\nBeta\ngamma beta\n")
    payload = store.grep(record.spill_id, "beta")
    assert payload["total_matches"] == 1
    payload = store.grep(record.spill_id, "beta", ignore_case=True)
    assert payload["total_matches"] == 2
    assert payload["matches"][0]["line"] == 2


def test_grep_respects_max_matches(store: SpillStore) -> None:
    record = store.put_text("\n".join("hit" for _ in range(10)))
    payload = store.grep(record.spill_id, "hit", max_matches=3)
    assert payload["returned_matches"] == 3
    assert payload["total_matches"] == 10


def test_grep_invalid_regex(store: SpillStore) -> None:
    record = store.put_text("abc")
    with pytest.raises(SpillError, match="regular expression"):
        store.grep(record.spill_id, "([")
    with pytest.raises(SpillError):
        store.grep(record.spill_id, "a", max_matches=0)


def test_grep_rejects_nested_quantifiers_redos(store: SpillStore) -> None:
    record = store.put_text("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa!")
    with pytest.raises(SpillError, match="nested repetitions"):
        store.grep(record.spill_id, "(a+)+")
    with pytest.raises(SpillError, match="nested repetitions"):
        store.grep(record.spill_id, "([0-9]+)+")
    with pytest.raises(SpillError, match="nested repetitions"):
        store.grep(record.spill_id, "(a*)*")
    with pytest.raises(SpillError, match="nested repetitions"):
        store.grep(record.spill_id, "(a|b+)+")
    with pytest.raises(SpillError, match="nested repetitions"):
        store.grep(record.spill_id, "((a+))+")


def test_grep_rejects_ambiguous_alternation_redos(store: SpillStore) -> None:
    # Nested repetitions are not the only exponential shape: a repeated group
    # whose alternatives start with the same character backtracks just as badly,
    # and the per-line deadline can never interrupt one in-flight ``re`` match.
    record = store.put_text("a" * 400 + "!")
    ambiguous = (
        "(a|aa)+$",          # one alternative is a prefix of the other
        "(foo|foobar)+$",    # same, spelled out
        "(a|)+$",            # one branch can match nothing
        "(a|ab|b)+$",        # two alternatives share their first character
        r"(\d|ab)+$",        # open-ended character set next to a literal
    )
    for pattern in ambiguous:
        with pytest.raises(SpillError, match="alternatives"):
            store.grep(record.spill_id, pattern)


def test_grep_rejects_ambiguous_alternation_with_ignore_case(store: SpillStore) -> None:
    record = store.put_text("a" * 400 + "!")
    with pytest.raises(SpillError, match="alternatives"):
        store.grep(record.spill_id, "(abc|ABD)+$", ignore_case=True)


def test_grep_keeps_working_for_safe_alternations_and_classes(store: SpillStore) -> None:
    record = store.put_text("alpha\nbeta\n")
    payload = store.grep(record.spill_id, "(alpha|beta)+")
    assert payload["total_matches"] == 2
    # The recommended rewrite for an ambiguous branch: one character class.
    payload = store.grep(record.spill_id, r"^([\w.-])+$")
    assert payload["total_matches"] == 2
    # Bounded repetition cannot blow up, so an ambiguous group stays allowed.
    assert store.grep(record.spill_id, "(a|aa){1,3}")["total_matches"] == 2


def test_grep_checks_deadline_per_line(store: SpillStore, monkeypatch: pytest.MonkeyPatch) -> None:
    lines = "\n".join(f"line-{i} target" for i in range(10))
    record = store.put_text(lines)
    calls = 0
    real_monotonic = time.monotonic

    def fake_monotonic() -> float:
        nonlocal calls
        calls += 1
        return real_monotonic() + (100.0 if calls > 2 else 0.0)

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    with pytest.raises(SpillError, match="budget"):
        store.grep(record.spill_id, "target", budget_seconds=1.0)


def test_read_and_grep_file_size_limits(store: SpillStore, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_READ_FILE_BYTES == 50 * 1024 * 1024
    assert MAX_GREP_FILE_BYTES == 50 * 1024 * 1024
    record = store.put_text("content")

    real_stat = Path.stat

    def fake_stat(self: Path, **kwargs: Any) -> os.stat_result:
        res = real_stat(self, **kwargs)
        if self == record.path:
            return os.stat_result((
                res.st_mode,
                res.st_ino,
                res.st_dev,
                res.st_nlink,
                res.st_uid,
                res.st_gid,
                60 * 1024 * 1024,
                res.st_atime,
                res.st_mtime,
                res.st_ctime,
            ))
        return res

    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(SpillError, match="too large to read"):
        store.read(record.spill_id)
    with pytest.raises(SpillError, match="too large to grep"):
        store.grep(record.spill_id, "content")


def test_read_past_eof(store: SpillStore) -> None:
    record = store.put_text("short")
    payload = store.read(record.spill_id, offset=100, limit=10)
    assert payload["text"] == ""
    assert payload["returned_chars"] == 0
    assert payload["eof"] is True


def test_resolve_rejects_traversal(store: SpillStore, tmp_path: Path) -> None:
    store.ensure()
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    with pytest.raises(SpillError):
        store.resolve("../outside.txt")
    with pytest.raises(SpillError):
        store.resolve(str(tmp_path / "outside.txt"))
    with pytest.raises(SpillError):
        store.resolve("")


def test_resolve_accepts_absolute_path_inside_store(store: SpillStore) -> None:
    record = store.put_text("inside")
    assert store.resolve(str(record.path)) == record.path


def test_resolve_accepts_unique_id_prefix(store: SpillStore) -> None:
    record = store.put_text("prefix me")
    assert store.resolve(record.spill_id[:12]) == record.path


def test_resolve_rejects_ambiguous_prefix(store: SpillStore, tmp_path: Path) -> None:
    store.ensure()
    (store.directory / "abcdef12aa.txt").write_text("one", encoding="utf-8")
    (store.directory / "abcdef12bb.txt").write_text("two", encoding="utf-8")
    with pytest.raises(SpillError, match="ambiguous"):
        store.resolve("abcdef12")


def test_resolve_unknown_id(store: SpillStore) -> None:
    store.ensure()
    with pytest.raises(SpillError, match="unknown spill id"):
        store.resolve("0" * 64)


def test_resolve_directory_is_rejected(store: SpillStore) -> None:
    store.ensure()
    with pytest.raises(SpillError):
        store.resolve(str(store.directory))


def test_resolve_follows_symlink_out_of_store(store: SpillStore, tmp_path: Path) -> None:
    store.ensure()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = store.directory / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - no symlink rights
        pytest.skip("symlinks unavailable")
    with pytest.raises(SpillError):
        store.resolve("link.txt")


def test_directory_with_spaces_and_unicode(tmp_path: Path) -> None:
    store = SpillStore(tmp_path / "spill dir ü nit")
    record = store.put_text("x" * 10)
    assert record.path.parent.name == "spill dir ü nit"
    assert record.path.exists()
    assert store.read(record.spill_id, limit=5)["text"] == "xxxxx"


def test_resolve_rejects_paths_on_another_windows_drive(
    store: SpillStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = store.put_text("inside")

    def different_drive(*args: object, **kwargs: object) -> str:
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr("mcp_output_guard.spill.os.path.commonpath", different_drive)
    with pytest.raises(SpillError, match="outside the spill directory"):
        store.resolve(str(record.path))


def test_list_and_stats(store: SpillStore) -> None:
    assert store.list() == []
    record = store.put_text("listed", tool="t1")
    listed = store.list()
    assert len(listed) == 1
    assert listed[0]["spill_id"] == record.spill_id
    assert listed[0]["path"].endswith(".txt")
    stats = store.stats()
    assert stats["files"] == 1
    assert stats["bytes"] > 0


def test_list_ignores_corrupt_sidecar(store: SpillStore) -> None:
    store.ensure()
    (store.directory / "deadbeef.meta.json").write_text("{not json", encoding="utf-8")
    assert store.list() == []


def test_cleanup_removes_only_old_files(store: SpillStore) -> None:
    old = store.put_text("old")
    fresh = store.put_text("fresh")
    long_ago = time.time() - 10_000
    os.utime(old.path, (long_ago, long_ago))
    os.utime(store.directory / f"{old.spill_id}.meta.json", (long_ago, long_ago))
    removed = store.cleanup(ttl_seconds=3600)
    assert removed >= 1
    assert fresh.path.exists()


def test_cleanup_on_missing_directory(tmp_path: Path) -> None:
    assert SpillStore(tmp_path / "nope").cleanup() == 0


def test_cleanup_negative_ttl_is_noop(store: SpillStore) -> None:
    store.put_text("keep")
    assert store.cleanup(ttl_seconds=-1) == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
def test_ensure_does_not_chmod_an_existing_directory(tmp_path: Path) -> None:
    # --spill-dir may point at a directory the user prepared; adjusting its
    # permissions behind their back is surprising and can widen or break ACLs.
    existing = tmp_path / "prepared"
    existing.mkdir()
    os.chmod(existing, 0o755)
    SpillStore(existing).ensure()
    assert stat.S_IMODE(existing.stat().st_mode) == 0o755


def test_ensure_reports_unwritable_directory(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    store = SpillStore(blocker / "sub")
    with pytest.raises(SpillError, match="cannot create spill directory"):
        store.ensure()


def test_ensure_probes_writability_and_rejects_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SpillStore(tmp_path / "probe_target")
    # Verify normal ensure succeeds and probe is cleaned up
    assert store.ensure().exists()
    assert not any(p.name.startswith(".probe_") for p in store.directory.iterdir())

    # Simulate directory where files cannot be written (PermissionError / OSError)
    real_write = Path.write_bytes

    def fake_write_bytes(self: Path, data: bytes) -> int:
        if self.parent == store.directory and self.name.startswith(".probe_"):
            raise PermissionError("Access denied: read-only directory")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", fake_write_bytes)
    with pytest.raises(SpillError, match="is not writable"):
        store.ensure()


def test_default_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MCP_OUTPUT_GUARD_SPILL_DIR", str(tmp_path / "custom"))
    assert default_dir() == tmp_path / "custom"
    monkeypatch.delenv("MCP_OUTPUT_GUARD_SPILL_DIR")
    assert default_dir().name == "spills"
    assert ".mcp-guard" in str(default_dir())


def test_default_store_is_a_singleton(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from mcp_output_guard import spill as spill_module

    monkeypatch.setattr(spill_module, "_DEFAULT", None)
    monkeypatch.setenv("MCP_OUTPUT_GUARD_SPILL_DIR", str(tmp_path / "singleton"))
    first = default_store()
    assert default_store() is first
    assert first.directory == tmp_path / "singleton"


def test_ttl_default_is_a_week() -> None:
    assert DEFAULT_TTL_SECONDS == 7 * 24 * 3600


def test_spill_id_is_sha256_of_content(store: SpillStore) -> None:
    import hashlib

    record = store.put_text("abc")
    assert record.spill_id == hashlib.sha256(b"abc").hexdigest()


def test_record_as_dict(store: SpillStore) -> None:
    record = store.put_text("x", tool="t")
    payload = record.as_dict()
    assert payload["tool"] == "t"
    assert payload["kind"] == "text"


def test_spill_dir_with_spaces_and_unicode(tmp_path: Path) -> None:
    """A ``C:\\Program Files``-style spill directory must work everywhere.

    Windows user directories routinely carry spaces and non-ASCII characters,
    so the store must not assume a POSIX-clean path.
    """
    directory = tmp_path / "Program Files" / "çıktı klasörü" / "spills"
    store = SpillStore(directory)
    record = store.put_text("merhaba dünya")
    assert record.path.parent == directory
    assert store.resolve(record.spill_id) == record.path
    assert store.read(str(record.path))["text"] == "merhaba dünya"


def test_resolve_rejects_other_drive(
    store: SpillStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows ``os.path.commonpath`` raises for paths on different drives.

    That ValueError must become a containment refusal, not a crash, so an
    absolute ``D:\\leak.txt`` reference is denied exactly like ``../leak.txt``.
    """
    record = store.put_text("inside")

    def boom(paths: object) -> str:
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr("mcp_output_guard.spill.os.path.commonpath", boom)
    with pytest.raises(SpillError, match="outside the spill directory"):
        store.resolve(str(record.path))
