# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Spill store writability probe at startup: `store.ensure()` actively checks that files can be
  created, failing immediately at startup (`exit code 2`) if unwritable; aligned `SECURITY.md`
  and documentation between startup validation and runtime fail-open.
- Windows virtualenv discovery in `scripts/check.sh`: added candidate scanning and bootstrap
  support for `Scripts/python.exe`, enabling zero-setup developer gate execution on Windows.
- Catastrophic backtracking defense in `output_guard_grep`: added static rejection of nested
  quantifiers (ReDoS risk), per-line wall-clock deadline checks, and 4,000-character line bounds.
- `output_guard_grep` now also rejects an unbounded repetition of a group whose alternatives can
  match the same text (`(a|aa)+`, `(foo|foobar)*`, a branch that can match nothing) and any
  repeated branch whose first characters the analyser cannot bound (for example `(\d|x)+`).
  Rejecting nested quantifiers alone left a hang reachable on a single 4,000-character line,
  where the per-line deadline can never fire. Such patterns are refused up front with a message
  that names the fix (character class or disjoint alternatives); `(alpha|beta)+` and `[\w.-]+`
  keep working, and so do bounded repeats such as `(a|aa){1,3}`.

### Changed

- Memory streaming: `read` and `grep` now stream payloads in chunks/lines without buffering
  entire multi-megabyte spill files in RAM.
- Memory safety limits: enforced 50 MB maximum file size on `read`, `grep`, and in-memory `sql`,
  with a 500,000-row limit on CSV SQLite imports.
- Reproducible dependencies: `requirements-dev.lock` is now what CI installs
  (`pip install -r requirements-dev.lock`, then `pip install -e . --no-deps`) instead of the
  floating `.[dev]` range. `coverage` is pinned at 7.16.2, the newest release that still supports
  the project's Python 3.10 floor; a test asserts the workflow never falls back to `.[dev]`.
- JSON-RPC batches: virtual `output_guard_*` calls inside a batch are answered locally, in their
  own response array (matched by `id`), and only the remaining items are forwarded to the server.
  Previously they were forwarded and failed as unknown tools.
- Test coverage expanded to 222 tests (~92% branch+line coverage).

## [0.1.0] - 2026-11-03

First release.

### Added

- `mcp-output-guard run --max-chars N [--spill-dir D] -- <command>`: a stdio proxy that
  guards every `tools/call` result from any MCP server.
- Text results above the limit are cut to the first N characters and followed by a
  `[TRUNCATED: <total> chars total, <shown> shown, full output: <path>]` note; the complete
  text is written to a spill file.
- `truncated: true` plus the character counts, the spill path and a usage hint in
  `_meta.output_guard`, so the model can act on the cut instead of guessing.
- Results under the limit are forwarded byte for byte; `isError: true` results and JSON-RPC
  error responses are never rewritten.
- Cut points always fall on a character boundary, including multi-byte text and lone
  surrogate pairs.
- Multiple `content` blocks share one budget, in order; omitted blocks are counted in the note.
- `structuredContent` above the limit keeps its schema and the first K records; totals are
  reported in `_meta`. Nested lists are pruned largest-first, and an advertised `outputSchema`
  is respected (`minItems` kept, `prefixItems`/`contains` lists left untouched).
- Base64 image/audio blocks above `--image-max-chars` are decoded to real files and replaced
  with a pointer block, never sliced mid-payload.
- Virtual tools injected into `tools/list`: `output_guard_read`, `output_guard_grep`,
  `output_guard_sql` (read-only SQLite over spilled JSON/CSV) and `output_guard_spills`.
- Per-tool limits with `--tool-limit NAME=CHARS`.
- Spill store with SHA-256 file names, `0600` files, `0700` directory, path-traversal and
  symlink containment, TTL cleanup and listing.
- CLI: `spills list|stats|clean`, `read` with `--offset/--limit/--grep/--sql`.
- Windows support, including `cmd /c` wrapping for `.cmd`/`.bat` server shims such as `npx`.
- 195 tests, 93% line+branch coverage, ruff and mypy in CI on Linux and Windows.

[0.1.0]: https://github.com/YusufBeratZengin/mcp-output-guard/releases/tag/v0.1.0
