# Security Policy

## Supported versions

Only the latest released minor version receives fixes.

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |

## Reporting a vulnerability

Open a private advisory at
<https://github.com/YusufBeratZengin/mcp-output-guard/security/advisories/new>, or email the
maintainer listed in `pyproject.toml`. Please do not open a public issue for a vulnerability.

Include the version, the platform, a reproduction and the impact you believe it has. You can
expect an initial reply within seven days. This is a volunteer project; there is no bounty.

## What this tool does with your data

`mcp-output-guard` sits between an MCP client and an MCP server on the same machine. It:

- reads and writes JSON-RPC messages on stdin/stdout of the two processes;
- writes oversized payloads to the spill directory (`~/.mcp-guard/spills` by default);
- never makes a network connection, never phones home, never sends telemetry.

## Known properties and risks

- **Spill files contain whatever the server returned.** Tool output routinely includes
  environment variables, database rows, tokens echoed into logs or customer records. Files are
  created with mode `0600` inside a `0700` directory, but the contents are not encrypted and not
  redacted. Treat the spill directory as sensitive, point `--spill-dir` at a location with the
  right retention policy, and run `mcp-output-guard spills clean` on a schedule.
- **Spill references are contained.** `output_guard_read`, `output_guard_grep` and
  `output_guard_sql` resolve a path, then require it to stay inside the spill directory; `..`,
  absolute paths outside it and symlinks pointing outside it are refused.
- **`output_guard_sql` is read-only.** Only `SELECT`, `WITH` and `EXPLAIN` are accepted, a single
  statement is allowed, and the SQLite connection is switched to `query_only` before the query
  runs. The data lives in an in-memory database, so spilled files are never modified.
- **The proxy is not a sandbox.** It does not filter, validate or authorise the MCP server's
  behaviour; it only rewrites oversized results. Run untrusted MCP servers with the same care you
  would give any other local process.
- **Availability beats secrecy on ambiguity.** If a result cannot be spilled (unwritable
  directory), the command fails loudly at startup rather than silently passing oversized output
  through.

## Dependencies

There are no runtime dependencies. The development dependencies (pytest, ruff, mypy) are used
only in CI and are pinned by Dependabot.
