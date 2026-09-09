"""Architectural invariant test enforcing zero procedural else/elif statements."""
from __future__ import annotations

from pathlib import Path
import re

_DISALLOWED_PATTERN = re.compile(r"^\s*(else|elif)\s*:")


def test_zero_procedural_else_in_src() -> None:
    src_dir = Path("src/navi").resolve()
    py_files = sorted(src_dir.rglob("*.py"))
    assert len(py_files) > 0, "No python files found in src/navi"

    violations: list[str] = []
    for path in py_files:
        lines = path.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines, 1):
            if _DISALLOWED_PATTERN.match(line):
                violations.append(f"{path.relative_to(src_dir.parent.parent)}:{idx}: {line.strip()}")

    assert not violations, "Found procedural else/elif in src/navi:\n" + "\n".join(violations)


def test_zero_procedural_else_in_tests() -> None:
    tests_dir = Path("tests").resolve()
    py_files = sorted(tests_dir.glob("*.py"))
    assert len(py_files) > 0, "No python files found in tests"

    violations: list[str] = []
    for path in py_files:
        if path.name == "test_architecture_zero_else.py":
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines, 1):
            if _DISALLOWED_PATTERN.match(line):
                violations.append(f"{path.name}:{idx}: {line.strip()}")

    assert not violations, "Found procedural else/elif in tests:\n" + "\n".join(violations)
