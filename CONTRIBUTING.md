# Contributing

Thanks for taking the time to help. This project aims to stay small, dependency-free and
predictable, so a few rules carry more weight than usual.

## Getting set up

```bash
git clone https://github.com/YusufBeratZengin/mcp-output-guard
cd mcp-output-guard
python -m pip install -e ".[dev]"
bash scripts/check.sh
```

`scripts/check.sh` is the same gate CI runs: ruff, mypy and pytest with a hard 85% coverage
floor. A pull request that does not make it pass locally will not pass in CI either.

## What a good change looks like

- **A test that fails first.** For bug fixes, add the failing test (or paste the failing case in
  the issue) before the fix. The suite is expected to go red without the change.
- **No new runtime dependencies.** The package must stay installable with a bare Python 3.10+.
  If you believe a dependency is unavoidable, open an issue first and justify it in the PR.
- **The byte-for-byte contract is sacred.** If a response is not modified, it must be forwarded
  as the original bytes. Do not "clean up" JSON on the way through.
- **Never hide a truncation.** Any code path that drops data has to leave a note the model can
  read and a `_meta` entry a program can check.
- **Error paths are in scope.** Truncated output that cannot be spilled, unreadable spill
  directories, invalid base64, invalid regexes and SQL writes all have explicit behaviour and
  tests. New code should do the same.
- **Type everything.** `mypy` runs with `disallow_untyped_defs`.

## Style

- `ruff` decides formatting and import order; run `python -m ruff check --fix src tests`.
- Line length 100.
- Comments explain *why* (a protocol quirk, a Windows detail), not *what*.

## Commit messages

Short imperative subject lines, English, under ~72 characters:

```
Guard structuredContent without touching the schema
Fix note length when the spill path is long
```

## Reporting bugs

Please include:

1. the proxy command line (with the server name, not its secrets),
2. the client and its version,
3. the smallest tool result that reproduces it — a fake payload is fine,
4. what you expected versus what came out.

Do not paste real tool output that may contain credentials. Redact it; `xxxxx` placeholders are
readable enough.

## Security issues

Please do not open a public issue for a security problem. See [SECURITY.md](SECURITY.md).

## Code of conduct

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
