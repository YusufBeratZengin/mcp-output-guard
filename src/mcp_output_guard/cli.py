"""Command line interface.

    mcp-output-guard run --max-chars 20000 -- npx -y @modelcontextprotocol/server-filesystem .
    mcp-output-guard spills list
    mcp-output-guard read <spill-id> --offset 0 --limit 4000
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .limits import DEFAULT_IMAGE_MAX_CHARS, ToolLimits
from .proxy import Proxy
from .spill import SpillError, SpillStore, default_dir
from .spill_sql import SpillSQLError

__all__ = ["build_parser", "main"]

PROG = "mcp-output-guard"


def _store_from_args(args: argparse.Namespace) -> SpillStore:
    directory = Path(args.spill_dir) if getattr(args, "spill_dir", None) else default_dir()
    return SpillStore(directory, ttl_seconds=getattr(args, "ttl_seconds", 7 * 24 * 3600))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Guards an MCP stdio server: oversized tool results are truncated with a "
            "visible marker and the full payload is spilled to a local file."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run an MCP server behind the guard")
    run.add_argument("--max-chars", type=int, required=True, help="max text characters per tool result")
    run.add_argument("--spill-dir", help="where full payloads are written (default ~/.mcp-guard/spills)")
    run.add_argument(
        "--tool-limit",
        action="append",
        default=[],
        metavar="NAME=CHARS",
        help="per-tool character limit; repeatable",
    )
    run.add_argument(
        "--image-max-chars",
        type=int,
        default=DEFAULT_IMAGE_MAX_CHARS,
        help=f"base64 payloads above this many characters are spilled to a file (default {DEFAULT_IMAGE_MAX_CHARS})",
    )
    run.add_argument("--note-lang", choices=sorted({"en", "tr"}), default="en", help="language of the truncation note")
    run.add_argument(
        "--reserve-note",
        action="store_true",
        help="count the truncation note inside --max-chars instead of appending it",
    )
    run.add_argument("--ttl-seconds", type=int, default=7 * 24 * 3600, help="spill retention for cleanup")
    run.add_argument("--no-virtual-tools", action="store_true", help="do not inject output_guard_* tools")
    run.add_argument("--stats", action="store_true", help="print a usage summary to stderr on exit")
    run.add_argument(
        "server",
        nargs=argparse.REMAINDER,
        help="-- followed by the MCP server command",
    )

    spills = sub.add_parser("spills", help="inspect or clean the spill directory")
    spills.add_argument("action", choices=["list", "clean", "stats"])
    spills.add_argument("--spill-dir")
    spills.add_argument("--ttl-seconds", type=int, default=7 * 24 * 3600)
    spills.add_argument("--json", action="store_true", help="machine readable output")

    read = sub.add_parser("read", help="print a slice of a spill file")
    read.add_argument("ref", help="spill id or path")
    read.add_argument("--spill-dir")
    read.add_argument("--offset", type=int, default=0)
    read.add_argument("--limit", type=int, default=4000)
    read.add_argument("--grep", help="regex to search instead of reading a window")
    read.add_argument("--sql", help="read-only SQL query for JSON/CSV spills")

    return parser


def _run(args: argparse.Namespace) -> int:
    server = list(args.server)
    if server and server[0] == "--":
        server = server[1:]
    if not server:
        print(f"{PROG}: missing server command, use: {PROG} run --max-chars N -- <command>", file=sys.stderr)
        return 2
    if args.max_chars <= 0:
        print(f"{PROG}: --max-chars must be positive", file=sys.stderr)
        return 2
    try:
        limits = ToolLimits.parse(args.max_chars, args.tool_limit)
    except ValueError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2

    store = _store_from_args(args)
    try:
        store.ensure()
    except SpillError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2

    proxy = Proxy(
        server,
        max_chars=args.max_chars,
        store=store,
        tool_limits=limits,
        image_max_chars=args.image_max_chars,
        note_lang=args.note_lang,
        reserve_note=args.reserve_note,
        virtual_tools=not args.no_virtual_tools,
    )
    code = proxy.run()
    if args.stats:
        print(proxy.stats.summary(), file=sys.stderr)
    return code


def _spills(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    if args.action == "clean":
        removed = store.cleanup(ttl_seconds=args.ttl_seconds)
        print(f"removed {removed} spill file(s) from {store.directory}")
        return 0
    if args.action == "stats":
        stats = store.stats()
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print(f"{stats['files']} spill file(s), {stats['bytes']} bytes in {stats['directory']}")
        return 0
    records = store.list()
    if args.json:
        print(json.dumps(records, indent=2))
        return 0
    if not records:
        print(f"no spills in {store.directory}")
        return 0
    for record in records:
        print(
            f"{str(record.get('spill_id', ''))[:12]}  {int(record.get('size_bytes', 0)):>10} B  "
            f"{record.get('tool') or '-':<24}  {record.get('path')}"
        )
    return 0


def _read(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    try:
        if args.grep:
            payload: dict[str, Any] = store.grep(
                args.ref, args.grep, max_matches=max(1, args.limit)
            )
            for match in payload["matches"]:
                print(f"{match['line']}: {match['text']}")
            if payload["total_matches"] > payload["returned_matches"]:
                print(f"... {payload['total_matches']} matches total", file=sys.stderr)
            return 0
        if args.sql:
            from .spill_sql import query_spill

            path = store.resolve(args.ref)
            table = query_spill(path, args.sql, max_rows=max(1, args.limit))
            print("\t".join(table["columns"]))
            for row in table["rows"]:
                print("\t".join("" if value is None else str(value) for value in row))
            return 0
        payload = store.read(args.ref, offset=args.offset, limit=args.limit)
    except (SpillError, SpillSQLError) as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(payload["text"])
    if not payload["eof"]:
        print(f"\n... {payload['total_chars'] - (payload['offset'] + payload['returned_chars'])} chars left", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "run":
        return _run(args)
    if args.command == "spills":
        return _spills(args)
    if args.command == "read":
        return _read(args)
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
