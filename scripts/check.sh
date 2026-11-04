#!/usr/bin/env bash
# Full quality gate: lint, type check, tests and the coverage threshold.
# Run from anywhere:  bash scripts/check.sh
#
# Interpreter selection is deliberate. An explicit $PYTHON always wins. Otherwise
# the gate looks for an interpreter that can actually import the dev tools
# (ruff, mypy, pytest, pytest-cov) across PATH, common install prefixes and
# project-local virtualenvs. If none exists it provisions a project-local
# .venv and installs the dev extras instead of dying with three unrelated
# tracebacks. Set CHECK_BOOTSTRAP=0 to forbid the network step.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

has_dev_tools() {
  "$1" -c 'import ruff, mypy, pytest, pytest_cov' >/dev/null 2>&1
}

runs_python() {
  "$1" -c 'import sys' >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Build a deterministic candidate list: PATH names first (in priority order),
# then project virtualenvs, then anything versioned found under the usual
# install prefixes. Duplicates (same real path) are dropped.
# ---------------------------------------------------------------------------
candidates=()
seen_paths=""
add_candidate() {
  local path="$1" real
  [ -n "$path" ] || return 0
  if [ ! -x "$path" ]; then
    path=$(command -v "$path" 2>/dev/null) || return 0
  fi
  # Dedupe on the literal absolute path, not the symlink target: a virtualenv's
  # python is a symlink to the base interpreter but has a different sys.prefix,
  # so resolving it would hide the interpreter that actually has the dev tools.
  real=$(cd "$(dirname "$path")" 2>/dev/null && pwd)/$(basename "$path")
  case " $seen_paths " in
    *" $real "*) return 0 ;;
  esac
  seen_paths="$seen_paths $real"
  candidates+=("$path")
}

find_venv_python() {
  local dir="$1"
  if [ -f "$dir/bin/python" ]; then
    echo "$dir/bin/python"
  elif [ -f "$dir/Scripts/python.exe" ]; then
    echo "$dir/Scripts/python.exe"
  elif [ -f "$dir/Scripts/python" ]; then
    echo "$dir/Scripts/python"
  elif [ -f "$dir/bin/python3" ]; then
    echo "$dir/bin/python3"
  fi
}

if [ -n "${PYTHON:-}" ]; then
  add_candidate "$PYTHON"
  explicit_python=1
else
  explicit_python=0
  for name in python3 python python3.14 python3.13 python3.12 python3.11 python3.10; do
    add_candidate "$name"
  done
  for v in .venv venv; do
    add_candidate "$v/bin/python"
    add_candidate "$v/bin/python3"
    add_candidate "$v/Scripts/python.exe"
    add_candidate "$v/Scripts/python"
  done
  add_candidate "$HOME/.local/bin/python3"
  for pattern in \
    "$HOME/.local/share/uv/python"/*/bin/python3 \
    "$HOME/.pyenv/versions"/*/bin/python3 \
    /usr/local/bin/python3.* \
    /usr/bin/python3.* \
    /opt/*/bin/python3; do
    for path in $pattern; do
      add_candidate "$path"
    done
  done
fi

PYTHON=""
for candidate in "${candidates[@]}"; do
  if has_dev_tools "$candidate"; then
    PYTHON="$candidate"
    break
  fi
done

# ---------------------------------------------------------------------------
# No interpreter with the dev tools: provision .venv and install the extras.
# ---------------------------------------------------------------------------
if [ -z "$PYTHON" ] && [ "$explicit_python" = "1" ]; then
  target="${candidates[0]}"
  missing=""
  for module in ruff mypy pytest pytest_cov; do
    "$target" -c "import $module" >/dev/null 2>&1 || missing="$missing $module"
  done
  printf 'FAILED: %s cannot import:%s\n' "$target" "${missing:- (unknown)}" >&2
  printf 'Install the dev extras:  %s -m pip install -e ".[dev]"\n' "$target" >&2
  printf 'or point PYTHON at an interpreter that has them.\n' >&2
  exit 1
fi

if [ -z "$PYTHON" ]; then
  if [ "${CHECK_BOOTSTRAP:-1}" = "0" ]; then
    printf 'FAILED: no Python with ruff, mypy, pytest and pytest-cov was found.\n' >&2
    printf 'Install the dev extras:  python3 -m pip install -e ".[dev]"\n' >&2
    printf '(bootstrap disabled by CHECK_BOOTSTRAP=0)\n' >&2
    exit 1
  fi

  base=""
  for candidate in "${candidates[@]}"; do
    if runs_python "$candidate"; then
      base="$candidate"
      break
    fi
  done
  if [ -z "$base" ]; then
    printf 'FAILED: no working Python interpreter was found on PATH.\n' >&2
    exit 1
  fi

  printf 'No interpreter with the dev tools; bootstrapping .venv with %s (%s)\n' \
    "$base" "$("$base" --version 2>&1)" >&2

  venv_py="$(find_venv_python .venv)"
  if [ -z "$venv_py" ] || [ ! -f "$venv_py" ]; then
    if ! "$base" -m venv .venv >&2; then
      printf 'FAILED: could not create .venv with %s\n' "$base" >&2
      printf 'Install the dev extras:  %s -m pip install -e ".[dev]"\n' "$base" >&2
      exit 1
    fi
    venv_py="$(find_venv_python .venv)"
  fi

  if [ -z "$venv_py" ] || [ ! -f "$venv_py" ]; then
    printf 'FAILED: virtualenv was created but no Python binary found in .venv/bin or .venv/Scripts\n' >&2
    exit 1
  fi

  if command -v uv >/dev/null 2>&1; then
    install_cmd=(uv pip install --python "$venv_py" -e ".[dev]")
  else
    install_cmd=("$venv_py" -m pip install --disable-pip-version-check -q -e ".[dev]")
  fi

  if ! "${install_cmd[@]}" >&2; then
    printf 'FAILED: could not install the dev extras into .venv.\n' >&2
    printf 'Run manually:  %s -m pip install -e ".[dev]"\n' "$base" >&2
    exit 1
  fi

  if ! has_dev_tools "$venv_py"; then
    printf 'FAILED: %s still cannot import the dev tools.\n' "$venv_py" >&2
    exit 1
  fi
  PYTHON="$venv_py"
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

section "python: $($PYTHON --version 2>&1) ($PYTHON)"

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
