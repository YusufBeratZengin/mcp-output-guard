"""The CI configuration is part of the reproducibility promise in the README.

If a job installs the loose ``.[dev]`` extras again, the pinned versions in
``requirements-dev.lock`` stop describing what CI actually runs, and the claim
in the docs becomes untrue. This test keeps the two in step.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
LOCK = ROOT / "requirements-dev.lock"

DEV_TOOLS = ("pytest", "pytest-cov", "ruff", "mypy")


def test_workflow_uses_the_pinned_lock_file() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "requirements-dev.lock" in workflow
    # No job may fall back to the floating extras range.
    assert "\".[dev]\"" not in workflow
    for job_line in workflow.splitlines():
        if "pip install -e" in job_line:
            assert "--no-deps" in job_line, job_line


def test_matrix_covers_the_declared_python_floor() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    floor = re.search(r'requires-python\s*=\s*">=([0-9.]+)"', project)
    assert floor is not None
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert f'"{floor.group(1)}"' in workflow


def test_lock_pins_every_dev_tool_exactly() -> None:
    pinned = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.!+-]+)", line)
        assert match, f"unpinned requirement in requirements-dev.lock: {line}"
        pinned[match.group(1).lower().replace("_", "-")] = match.group(2)
    for tool in DEV_TOOLS:
        assert tool in pinned, tool
