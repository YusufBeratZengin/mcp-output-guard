#!/usr/bin/env bash
# Full quality gate: lint, type check, tests and the coverage threshold.
# Run from anywhere:  bash scripts/check.sh
#
# The interpreter is chosen deliberately: an explicit $PYTHON wins, otherwise
# the first candidate that can actually import the dev tools is used. This
# keeps the gate honest in environments where `python3` is a bare runtime
# without ruff/mypy/pytest (the failure then names the missing package instead
# of producing three unrelated tracebacks).
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

has_dev_tools() {
  "$1" -c 'import ruff, mypy, pytest' >/dev/null 2>&1
}

if [ -z "${PYTHON:-}" ]; then
  for candidate in python3 .venv/bin/python venv/bin/python; do
    if command -v "$candidate" >/dev/null 2>&1 && has_dev_tools "$candidate"; then
      PYTHON="$candidate"
      break
    fi
  done
  PYTHON="${PYTHON:-python3}"
fi

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

missing=""
for module in ruff mypy pytest; do
  if ! "$PYTHON" -c "import $module" >/dev/null 2>&1; then
    missing="$missing $module"
  fi
done
if [ -n "$missing" ]; then
  printf 'FAILED: %s cannot import:%s\n' "$PYTHON" "$missing" >&2
  printf 'Install the dev extras first:  %s -m pip install -e ".[dev]"\n' "$PYTHON" >&2
  printf 'or point PYTHON at an interpreter that has them.\n' >&2
  exit 1
fi

section "lint (ruff)"
if "$PYTHON" -m ruff check src tests examples; then :; else fail "ruff"; fi

section "types (mypy)"
if "$PYTHON" -m mypy; then :; else fail "mypy"; fi

section "tests (pytest + coverage >= 85%)"
if "$PYTHON" -m pytest -q --cov=mcp_output_guard --cov-fail-under=85 --cov-report=term-missing; then
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
