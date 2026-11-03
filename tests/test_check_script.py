"""The quality gate itself has to be robust, since every other check depends on it."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")


def _run(env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTHON", None)
    env.pop("CHECK_BOOTSTRAP", None)
    env.update(env_overrides)
    return subprocess.run(
        [BASH, str(SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_script_is_syntactically_valid() -> None:
    assert BASH is not None
    result = subprocess.run(
        [BASH, "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_explicit_python_without_dev_tools_reports_every_missing_package() -> None:
    # /bin/false cannot import anything, so the explicit interpreter must win,
    # name all four tools and never silently fall back to a different python.
    result = _run({"PYTHON": "/bin/false"})
    assert result.returncode == 1
    stderr = result.stderr
    assert "cannot import" in stderr
    for module in ("ruff", "mypy", "pytest", "pytest_cov"):
        assert module in stderr
    assert 'pip install -e ".[dev]"' in stderr


def test_bootstrap_can_be_disabled_without_network() -> None:
    result = _run({"PYTHON": "/bin/false", "CHECK_BOOTSTRAP": "0"})
    assert result.returncode == 1
    # Explicit PYTHON keeps priority over the bootstrap switch: the message
    # still points at the interpreter the caller asked for.
    assert "/bin/false" in result.stderr
