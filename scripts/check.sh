#!/usr/bin/env bash
# Full quality gate: lint, type check, tests and the coverage threshold.
# Run from anywhere:  bash scripts/check.sh
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

PYTHON="${PYTHON:-python3}"
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
status=0

section() {
  printf '\n== %s\n' "$*"
}

fail() {
  printf 'FAILED: %s\n' "$*" >&2
  status=1
}

section "python: $($PYTHON --version 2>&1)"

section "lint (ruff)"
if $PYTHON -m ruff check src tests examples; then :; else fail "ruff"; fi

section "types (mypy)"
if $PYTHON -m mypy; then :; else fail "mypy"; fi

section "tests (pytest + coverage >= 85%)"
if $PYTHON -m pytest -q --cov=mcp_output_guard --cov-fail-under=85 --cov-report=term-missing; then
  :
else
  fail "pytest"
fi

section "result"
if [ "$status" -eq 0 ]; then
  printf 'OK: lint, types, tests and coverage all passed\n'
else
  printf 'FAILED: see the messages above\n' >&2
fi
exit "$status"
