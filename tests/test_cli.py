"""CLI tests: argument handling, the read/spills subcommands and error paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_output_guard import __version__
from mcp_output_guard.cli import main
from mcp_output_guard.spill import SpillStore


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage:" in capsys.readouterr().out


def test_run_without_server_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--max-chars", "100"]) == 2
    assert "missing server command" in capsys.readouterr().err


def test_run_with_bad_max_chars(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--max-chars", "0", "--", "server"]) == 2
    assert "positive" in capsys.readouterr().err


def test_run_with_bad_tool_limit(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--max-chars", "100", "--tool-limit", "oops", "--", "server"]) == 2
    assert "NAME=CHARS" in capsys.readouterr().err


def test_run_with_unwritable_spill_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    code = main(["run", "--max-chars", "100", "--spill-dir", str(blocker), "--", "server"])
    assert code == 2
    assert "spill directory" in capsys.readouterr().err


def test_spills_list_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["spills", "list", "--spill-dir", str(tmp_path / "s")]) == 0
    assert "no spills" in capsys.readouterr().out


def test_spills_list_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = SpillStore(tmp_path / "s")
    store.put_text("hello", tool="demo")
    assert main(["spills", "list", "--spill-dir", str(store.directory)]) == 0
    out = capsys.readouterr().out
    assert "demo" in out

    assert main(["spills", "list", "--spill-dir", str(store.directory), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["tool"] == "demo"


def test_spills_stats(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = SpillStore(tmp_path / "s")
    store.put_text("hello")
    assert main(["spills", "stats", "--spill-dir", str(store.directory)]) == 0
    assert "1 spill file" in capsys.readouterr().out
    assert main(["spills", "stats", "--spill-dir", str(store.directory), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["files"] == 1


def test_spills_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = SpillStore(tmp_path / "s")
    store.put_text("hello")
    assert main(["spills", "clean", "--spill-dir", str(store.directory), "--ttl-seconds", "0"]) == 0
    assert "removed" in capsys.readouterr().out


def test_read_prints_window(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = SpillStore(tmp_path / "s")
    record = store.put_text("0123456789")
    assert main(["read", record.spill_id, "--spill-dir", str(store.directory), "--limit", "4"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "0123"
    assert "chars left" in captured.err


def test_read_grep(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = SpillStore(tmp_path / "s")
    record = store.put_text("alpha\nbeta\n")
    assert main(["read", record.spill_id, "--spill-dir", str(store.directory), "--grep", "beta"]) == 0
    assert "2: beta" in capsys.readouterr().out


def test_read_sql(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = SpillStore(tmp_path / "s")
    record = store.put_json([{"id": 1}, {"id": 2}])
    assert (
        main(
            [
                "read",
                record.spill_id,
                "--spill-dir",
                str(store.directory),
                "--sql",
                "SELECT count(*) AS n FROM data",
            ]
        )
        == 0
    )
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["n", "2"]


def test_read_unknown_spill(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["read", "missing.txt", "--spill-dir", str(tmp_path / "s")]) == 1
    assert "not found" in capsys.readouterr().err


def test_read_bad_sql(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = SpillStore(tmp_path / "s")
    record = store.put_json([{"id": 1}])
    code = main(
        ["read", record.spill_id, "--spill-dir", str(store.directory), "--sql", "DELETE FROM data"]
    )
    assert code == 1
    assert "SELECT" in capsys.readouterr().err


def test_run_wires_options_into_the_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}

    class FakeProxy:
        def __init__(self, command, **kwargs):  # type: ignore[no-untyped-def]
            captured["command"] = command
            captured.update(kwargs)

            class Stats:
                def summary(self) -> str:
                    return "stats-line"

            self.stats = Stats()

        def run(self) -> int:
            return 0

    monkeypatch.setattr("mcp_output_guard.cli.Proxy", FakeProxy)
    code = main(
        [
            "run",
            "--max-chars",
            "123",
            "--spill-dir",
            str(tmp_path / "spills"),
            "--tool-limit",
            "big=9",
            "--note-lang",
            "tr",
            "--reserve-note",
            "--stats",
            "--",
            "fake-server",
            "--flag",
        ]
    )
    assert code == 0
    assert captured["command"] == ["fake-server", "--flag"]
    assert captured["max_chars"] == 123
    assert captured["note_lang"] == "tr"
    assert captured["reserve_note"] is True
    assert captured["virtual_tools"] is True
    assert captured["tool_limits"].for_tool("big") == 9
    assert "stats-line" in capsys.readouterr().err


def test_run_accepts_command_without_double_dash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    class FakeProxy:
        def __init__(self, command, **kwargs):  # type: ignore[no-untyped-def]
            seen["command"] = command

            class Stats:
                def summary(self) -> str:
                    return ""

            self.stats = Stats()

        def run(self) -> int:
            return 0

    monkeypatch.setattr("mcp_output_guard.cli.Proxy", FakeProxy)
    assert main(["run", "--max-chars", "10", "server-binary"]) == 0
    assert seen["command"] == ["server-binary"]
