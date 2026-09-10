"""Architectural invariant test enforcing zero procedural else/elif statements via AST."""
from __future__ import annotations

import ast
from pathlib import Path


def test_zero_procedural_else_in_src() -> None:
    src_dir = Path("src/navi").resolve()
    py_files = sorted(src_dir.rglob("*.py"))
    assert len(py_files) > 0, "No python files found in src/navi"

    violations: list[str] = []
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and node.orelse:
                branch_type = "else"
                if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                    branch_type = "elif"
                violations.append(f"{path.relative_to(src_dir.parent.parent)}:{node.lineno}: ({branch_type})")

    assert not violations, "Found procedural else/elif in src/navi:\n" + "\n".join(violations)


def test_zero_procedural_else_in_tests() -> None:
    tests_dir = Path("tests").resolve()
    py_files = sorted(tests_dir.glob("*.py"))
    assert len(py_files) > 0, "No python files found in tests"

    violations: list[str] = []
    for path in py_files:
        if path.name == "test_architecture_zero_else.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and node.orelse:
                branch_type = "else"
                if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                    branch_type = "elif"
                violations.append(f"{path.name}:{node.lineno}: ({branch_type})")

    assert not violations, "Found procedural else/elif in tests:\n" + "\n".join(violations)


def test_zero_inline_ternary_in_src() -> None:
    src_dir = Path("src/navi").resolve()
    py_files = sorted(src_dir.rglob("*.py"))
    violations: list[str] = []
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.IfExp):
                violations.append(f"{path.relative_to(src_dir.parent.parent)}:{node.lineno}")

    assert not violations, "Found inline ternary (ast.IfExp) in src/navi:\n" + "\n".join(violations)


def test_zero_inline_ternary_in_tests() -> None:
    tests_dir = Path("tests").resolve()
    py_files = sorted(tests_dir.glob("*.py"))
    violations: list[str] = []
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.IfExp):
                violations.append(f"{path.name}:{node.lineno}")

    assert not violations, "Found inline ternary (ast.IfExp) in tests:\n" + "\n".join(violations)
