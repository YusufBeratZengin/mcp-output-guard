# mcp-output-guard

**Stops oversized MCP tool output from flooding your agent's context window: it trims the
result, writes the full payload to a local file, and tells the model exactly what was cut and
where to find it.**

A stdio proxy for any [Model Context Protocol](https://modelcontextprotocol.io) server. Point
your client at the proxy instead of the server; no server changes, no SDK, no dependencies —
Python standard library only.

```
your MCP client  ──stdio──▶  mcp-output-guard  ──stdio──▶  any MCP server
                                   │
                                   └──▶  ~/.mcp-guard/spills/<sha256>.txt   (full payload)
```

## Why

Agents die on tool output, not on prompts. A database query returns 40 000 rows, a browser
snapshot returns 3 MB of DOM, a log reader returns 50 MB — and the session either blows the
context window or gets silently clipped:

- Claude Code caps tool results at roughly 25K tokens and writes beyond that to a file
  ([anthropics/claude-code#45770](https://github.com/anthropics/claude-code/issues/45770)).
- Storybook's MCP server overruns its output limit on large repositories
  ([storybookjs/mcp#311](https://github.com/storybookjs/mcp/issues/311)).
- LM Studio truncates responses above 50K characters with a bare `(truncated)`
  ([lmstudio-ai/lmstudio-bug-tracker#2475](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/2475)).
- Google AI Edge Gallery crashes the chat on unbounded results
  ([google-ai-edge/gallery#889](https://github.com/google-ai-edge/gallery/issues/889)).

Every one of those is fixed on the *server* side, or not at all. `mcp-output-guard` fixes it on
the *client* side, for any server, and never hides the cut: the model gets a `truncated: true`
flag, the total size, and a path.

Existing options are narrower: `mcp-cache` caches, `mcp-rlm-proxy` proxies without structured
truncation metadata, and FastMCP's `ResponseLimitingMiddleware` only works if the server was
written with FastMCP — it cannot guard a third-party server you do not control.

## Install

```bash
pipx install mcp-output-guard      # once published; today: pip install .
```

From a checkout: `pip install .` (or `python -m pip install -e ".[dev]"` for development).
Requires Python 3.10+. No runtime dependencies.

## 30-second example

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"read_logs"}}' \
  | mcp-output-guard run --max-chars 400 -- python examples/demo_server.py
```

Real output (pretty-printed for readability, the tool returned 37 149 characters):

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "2026-01-01T00:00:00Z INFO worker-0 job=fake-job-000 host=fake-host-0 status=ok latency_ms=0\n2026-01-01T00:00:01Z INFO worker-1 job=fake-job-001 host=fake-host-1 status=ok latency_ms=1\n... (400 chars)"
      },
      {
        "type": "text",
        "text": "\n\n[TRUNCATED: 37149 chars total, 400 shown, full output: /home/you/.mcp-guard/spills/85650bc82f3ddeeddca95634c6cda8a95fea5ab278bab1631b3f7f62a02c7489.txt]"
      }
    ],
    "_meta": {
      "truncated": true,
      "output_guard": {
        "truncated": true,
        "tool": "read_logs",
        "max_chars": 400,
        "total_chars": 37149,
        "shown_chars": 400,
        "spill": "/home/you/.mcp-guard/spills/85650bc82f3ddeeddca95634c6cda8a95fea5ab278bab1631b3f7f62a02c7489.txt",
        "structured_content": "absent",
        "hint": "The full output is on disk. Use output_guard_read(spill, offset, limit) to page through it, output_guard_grep(spill, pattern) to search it, or output_guard_sql(spill, query) if it is JSON/CSV."
      }
    }
  }
}
```

Run the whole thing yourself with `bash examples/demo.sh` (three calls: truncate, page, grep).

The file holds every one of the 37 149 characters. The model can page through it without
leaving the conversation, because the proxy also injects four tools into `tools/list`:

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"output_guard_read",
  "arguments":{"spill":"85650bc8...7489.txt","offset":0,"limit":200}}}
```

```
/home/you/.mcp-guard/spills/85650bc8....txt
chars 0..200 of 37149 (eof=False)
---
2026-01-01T00:00:00Z INFO worker-0 job=fake-job-000 host=fake-host-0 status=ok latency_ms=0
...
```

## Use it with your client

Wrap the server command. Nothing else changes.

`claude_desktop_config.json` / `.mcp.json` / Cursor / Claude Code:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "mcp-output-guard",
      "args": ["run", "--max-chars", "20000", "--", "npx", "-y", "@modelcontextprotocol/server-filesystem", "/work"]
    }
  }
}
```

On Windows the proxy resolves `npx.cmd` shims automatically, so the same line works there.

## What it does to a result

| Situation | Behaviour |
| --- | --- |
| Result fits under the limit | Forwarded **byte for byte**, not re-serialized |
| Text over the limit | First N characters + a `[TRUNCATED: …]` note, full text spilled to a file |
| `isError: true` | Never touched — a mangled traceback is worse than a long one |
| JSON-RPC error response | Never touched |
| Several `content` blocks | The budget is shared in order; leftover blocks are counted in the note |
| Multi-byte text | Cut on a character boundary, never mid code point or mid surrogate pair |
| `structuredContent` over the limit | Lists trimmed largest-first, first K records kept (`minItems` honoured); when the schema pins the length (`prefixItems`/`contains`) or there is no list to trim, the field is **removed from the reply** and the complete result is in the JSON spill named by the note |
| Base64 image/audio over `--image-max-chars` | Decoded, written to a real file, replaced by a pointer + size |
| Broken base64 | Left exactly as it came, flagged in `_meta.output_guard.binary` |
| Spill directory unwritable at startup | **Fails loudly** with exit code 2 before running the server command |
| Spill directory full or unwritable mid-session | **Fails open**: the result is forwarded unguarded, a warning goes to stderr and the call is counted in `--stats` |

The byte-for-byte guarantee matters: a client that hashes or diffs tool output sees no change
unless the guard actually did something.

## Virtual tools

Added to every successful `tools/list` (disable with `--no-virtual-tools`):

| Tool | Purpose |
| --- | --- |
| `output_guard_read(spill, offset, limit)` | Character window, with `total_chars` and `eof` |
| `output_guard_grep(spill, pattern, ignore_case, max_matches)` | Regex search with line numbers |
| `output_guard_sql(spill, query, max_rows)` | Read-only SQL over a spilled JSON array or CSV |
| `output_guard_spills(limit)` | List recent spills |

`output_guard_sql` loads the payload into an in-memory SQLite table named `data`. Column types
are inferred, so `WHERE id > 15` is numeric, and the connection is switched to `query_only`
before the statement runs. Payloads are checked against a 50 MB in-memory safety limit.

`output_guard_grep` streams files line-by-line rather than reading entire payloads into memory.
Python cannot interrupt a single in-flight `re` match, so the protection that actually counts
happens *before* the search starts: a pattern with a quantifier nested inside another quantifier
(`(a+)+`), or an unbounded repetition of a group whose alternatives can match the same text
(`(a|aa)+`, `(foo|foobar)*`, a branch that can match nothing), is rejected at validation time.
So is any repeated branch the analyser cannot classify — an open-ended class such as `\d` or `.`
next to a literal — because a false "yes" means a wedged proxy. Rewrite those as a character
class (`^([\w.-])+$`) or make the alternatives disjoint. As a second layer, lines are capped at
4,000 characters and the wall-clock deadline (`budget_seconds`, default 5.0s) is checked before
each line.

## CLI

```
mcp-output-guard run --max-chars N [options] -- <server command>

  --max-chars N            text budget per tool result (required)
  --spill-dir D            where full payloads go (default ~/.mcp-guard/spills)
  --tool-limit NAME=N      per-tool override, repeatable
  --image-max-chars N      base64 payloads above this are spilled (default 1400000)
  --note-lang en|tr        language of the truncation note (default en)
  --reserve-note           count the note inside --max-chars instead of appending it
  --no-virtual-tools       do not inject output_guard_* tools
  --ttl-seconds N          retention used by `spills clean` (default 604800)
  --stats                  print a usage summary to stderr on exit

mcp-output-guard spills list|stats|clean [--json]
mcp-output-guard read <id|path> [--offset N] [--limit N] [--grep REGEX] [--sql QUERY]
```

The spill directory can also be set with `MCP_OUTPUT_GUARD_SPILL_DIR`.

## Use it as a library

```python
from mcp_output_guard import SpillStore, limit_result

store = SpillStore("/tmp/spills")
result, info = limit_result(
    {"content": [{"type": "text", "text": "x" * 50_000}]},
    4_000,
    spill=store,
    tool="db.query",
)
assert info["truncated"] and info["total_chars"] == 50_000
```

`result` is the same object when nothing had to change, and `info` is `None` — that is how the
proxy decides to forward the original bytes.

## Spill files are sensitive

Tool output routinely contains secrets: environment dumps, customer rows, tokens echoed in
logs. The guard:

- creates a spill directory it owns with mode `0700` and files with `0600` on POSIX systems (on
  Windows, `os.chmod` cannot alter NTFS ACLs and only toggles the read-only attribute; spill
  files inherit the directory's access control, so configure directory ACLs or point
  `--spill-dir` at an already-restricted location);
- leaves an existing `--spill-dir` exactly as the user prepared it: its permissions are not
  changed, only files written into it are tightened to `0600`;
- names files after the SHA-256 of their content (identical payloads deduplicate);
- refuses to read any path that resolves outside the spill directory, including via symlinks;
- can prune old files with `mcp-output-guard spills clean --ttl-seconds 3600`.

It cannot make the data safe. **Treat the spill directory as you would a log file containing
production data**, point `--spill-dir` at an encrypted or ephemeral volume if that matters, and
clean it up. Nothing is ever sent over the network by this tool.

## Limits and honesty

- Only the **stdio** transport is supported. HTTP/SSE servers are out of scope.
- Truncation is measured in **characters**, not tokens. A character budget is a proxy for
  tokens, not a measurement of them; CJK and code are denser than prose.
- **Windows file permissions**: Mode `0700` and `0600` permissions rely on POSIX bits. On Windows,
  `os.chmod` does not alter NTFS ACLs; spill files inherit the access control of their parent
  directory. Set explicit directory ACLs (e.g. using `icacls`) if multiple local users share
  the host.
- JSON-RPC **batches** are handled item by item: a virtual `output_guard_*` call inside a batch
  is answered locally (in its own response array, matched by `id`) and only the remaining items
  are forwarded, so a server never receives a tool it does not implement. Results of server calls
  inside a batch are guarded exactly like single calls. A batch that is partly answered locally
  is re-serialized, so the byte-for-byte guarantee covers single messages and untouched batches.
- Deeply nested `structuredContent` is pruned one list at a time, largest first; when the server
  advertises an `outputSchema`, `minItems` is honoured and length-pinned lists (`prefixItems`,
  `contains`) are left untrimmed. It is not a query engine for arbitrary schemas. When no list
  can be trimmed, the guard does not forward the payload oversized: it drops
  `structuredContent` from the reply, marks `_meta.output_guard.structured_content` as
  `spilled`, and the note points at the lossless JSON spill of the whole result.
- The proxy is single-process, single-connection, and does not multiplex.

## Development

```bash
python -m pip install -e ".[dev]"
bash scripts/check.sh        # ruff + mypy + pytest with --cov-fail-under=85
```

`check.sh` finds an interpreter that has the dev tools (or provisions `.venv` across POSIX or
Windows layouts and installs `.[dev]` into it), so running it without a prepared environment
works on both Linux and Windows; `PYTHON=...` forces one, `CHECK_BOOTSTRAP=0` disables the install step.

`bash examples/demo.sh` replays the walkthrough above against the bundled fake server and prints
the spill path, a paged read and a grep.

Current state: 222 tests, ~92% line+branch coverage, Linux and Windows CI. CI installs `requirements-dev.lock`
(`pip install -r requirements-dev.lock`, then `pip install -e . --no-deps`), so a green run is a run of
the pinned toolchain, not of whatever `pip` resolved that morning.

## License

MIT — see [LICENSE](LICENSE).
