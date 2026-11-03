# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- 192 tests, 93% line+branch coverage, ruff and mypy in CI on Linux and Windows.

[0.1.0]: https://github.com/YusufBeratZengin/mcp-output-guard/releases/tag/v0.1.0
