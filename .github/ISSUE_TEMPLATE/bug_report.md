---
name: Bug report
about: Something was truncated, mangled, or not truncated at all
labels: bug
---

## What happened

<!-- The smallest description that makes the bug obvious. -->

## Command line

```bash
# the proxy invocation, with the server name; no secrets
mcp-output-guard run --max-chars ... -- <server>
```

## Client and versions

- Client (Claude Desktop / Cursor / Claude Code / other):
- `mcp-output-guard --version`:
- Python version and OS:

## The result that reproduces it

<!--
A fake payload is perfect. Do not paste real tool output that may contain
credentials; replace values with xxxxx.
-->

```json
{"content": [{"type": "text", "text": "..."}]}
```

## Expected

## Actual

<!-- Include whether the response was rewritten at all, and what _meta said. -->

## Checklist

- [ ] I am on the latest release
- [ ] `bash scripts/check.sh` is green on my checkout (if I am running from source)
- [ ] I removed any secrets from the payloads above
