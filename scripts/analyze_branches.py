"""Branch and conditional analyzer using Python AST.

Measures:
1. Procedural branches (else: / elif: - ast.If with orelse)
2. Inline ternaries (ast.IfExp: x if cond else y)
3. Guard clauses (ast.If with empty orelse)
4. Historical comparison across git commits
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import sys
from typing import Any


@dataclass
class FileStats:
    path: str
    procedural_else: int = 0
    inline_ternary: int = 0
    guard_if: int = 0
    total_lines: int = 0
    ternary_details: list[dict[str, Any]] = field(default_factory=list)
    procedural_details: list[dict[str, Any]] = field(default_factory=list)


def analyze_source(source: str, rel_path: str = "") -> FileStats:
    stats = FileStats(path=rel_path, total_lines=len(source.splitlines()))
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return stats

    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            has_orelse = bool(node.orelse)
            stats.procedural_else += int(has_orelse)
            stats.guard_if += int(not has_orelse)
            if has_orelse:
                stats.procedural_details.append(
                    {
                        "line": getattr(node, "lineno", 0),
                        "type": "elif" if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If) else "else",
                    }
                )
        if isinstance(node, ast.IfExp):
            stats.inline_ternary += 1
            stats.ternary_details.append(
                {
                    "line": getattr(node, "lineno", 0),
                }
            )

    return stats


def analyze_directory(dir_path: Path) -> dict[str, FileStats]:
    results: dict[str, FileStats] = {}
    for py_path in sorted(dir_path.rglob("*.py")):
        rel = str(py_path.as_posix())
        try:
            content = py_path.read_text(encoding="utf-8")
        except Exception:
            continue
        results[rel] = analyze_source(content, rel_path=rel)
    return results


def get_git_files_at_commit(commit_ref: str, prefix: str = "src/navi") -> dict[str, FileStats]:
    cmd = ["git", "ls-tree", "-r", "--name-only", commit_ref, prefix]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return {}
    file_list = [line.strip() for line in proc.stdout.splitlines() if line.endswith(".py")]
    results: dict[str, FileStats] = {}
    for fpath in file_list:
        show_cmd = ["git", "show", f"{commit_ref}:{fpath}"]
        file_proc = subprocess.run(show_cmd, capture_output=True, text=True)
        if file_proc.returncode != 0:
            continue
        results[fpath] = analyze_source(file_proc.stdout, rel_path=fpath)
    return results


def format_summary(title: str, stats_map: dict[str, FileStats]) -> str:
    total_files = len(stats_map)
    total_proc = sum(s.procedural_else for s in stats_map.values())
    total_ternary = sum(s.inline_ternary for s in stats_map.values())
    total_guard = sum(s.guard_if for s in stats_map.values())
    total_lines = sum(s.total_lines for s in stats_map.values())

    out = [
        f"### {title}",
        f"- **Python Files**: {total_files}",
        f"- **Total Lines of Code**: {total_lines:,}",
        f"- **Procedural else/elif**: {total_proc}",
        f"- **Inline Ternaries (x if c else y)**: {total_ternary}",
        f"- **Guard Clauses (pure if without else)**: {total_guard}",
        f"- **Total Conditional Density**: {(total_proc + total_ternary + total_guard) / max(total_lines, 1) * 1000:.1f} per 1k LOC",
    ]
    return "\n".join(out)


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    src_dir = repo_root / "src" / "navi"
    tests_dir = repo_root / "tests"

    src_stats = analyze_directory(src_dir)
    tests_stats = analyze_directory(tests_dir)

    print("=" * 60)
    print("CURRENT WORKSPACE ANALYSIS")
    print("=" * 60)
    print(format_summary("src/navi", src_stats))
    print()
    print(format_summary("tests", tests_stats))
    print()

    # Module breakdown for src/navi
    modules: dict[str, dict[str, int]] = {}
    for path, s in src_stats.items():
        rel = Path(path).relative_to(src_dir)
        mod_name = rel.parts[0] if len(rel.parts) > 1 else "(root)"
        if mod_name not in modules:
            modules[mod_name] = {"files": 0, "proc": 0, "ternary": 0, "guard": 0, "lines": 0}
        modules[mod_name]["files"] += 1
        modules[mod_name]["proc"] += s.procedural_else
        modules[mod_name]["ternary"] += s.inline_ternary
        modules[mod_name]["guard"] += s.guard_if
        modules[mod_name]["lines"] += s.total_lines

    print("### Module Breakdown (src/navi)")
    print(f"{'Module':<25} | {'Files':<6} | {'Proc Else':<10} | {'Ternaries':<10} | {'Guard Ifs':<10} | {'Lines':<8}")
    print("-" * 80)
    for mod, data in sorted(modules.items(), key=lambda x: x[1]["ternary"] + x[1]["proc"], reverse=True):
        print(f"{mod:<25} | {data['files']:<6} | {data['proc']:<10} | {data['ternary']:<10} | {data['guard']:<10} | {data['lines']:<8}")
    print()

    # Top files with ternaries
    top_ternary = sorted(src_stats.values(), key=lambda x: x.inline_ternary, reverse=True)[:15]
    print("### Top 15 Files by Inline Ternary Count (src/navi)")
    print(f"{'File':<45} | {'Ternaries':<10} | {'Proc Else':<10} | {'Lines':<8}")
    print("-" * 75)
    for s in top_ternary:
        rel = Path(s.path).name
        print(f"{rel:<45} | {s.inline_ternary:<10} | {s.procedural_else:<10} | {s.total_lines:<8}")
    print()

    # Historical analysis
    milestones = [
        ("3d1ba39", "Baseline (before refactoring)"),
        ("9765f15", "Phase 1: Memory elimination"),
        ("6224ac0", "Phase 2: Branchless algebraic gating"),
        ("d671e2d", "Phase 3: State matrices & strategy"),
        ("8b27b1b", "Phase 4: 2D tables, dynamic params, zero-else test"),
        ("HEAD", "Current Working Tree"),
    ]

    print("=" * 60)
    print("HISTORICAL EVOLUTION (src/navi)")
    print("=" * 60)
    print(f"{'Commit':<10} | {'Phase / Milestone':<35} | {'Proc Else':<10} | {'Ternaries':<10} | {'Guard Ifs':<10}")
    print("-" * 85)
    for commit, desc in milestones:
        if commit == "HEAD":
            proc = sum(s.procedural_else for s in src_stats.values())
            ternary = sum(s.inline_ternary for s in src_stats.values())
            guard = sum(s.guard_if for s in src_stats.values())
            print(f"{'CURRENT':<10} | {desc:<35} | {proc:<10} | {ternary:<10} | {guard:<10}")
            continue
        c_stats = get_git_files_at_commit(commit, prefix="src/navi")
        proc = sum(s.procedural_else for s in c_stats.values())
        ternary = sum(s.inline_ternary for s in c_stats.values())
        guard = sum(s.guard_if for s in c_stats.values())
        print(f"{commit:<10} | {desc:<35} | {proc:<10} | {ternary:<10} | {guard:<10}")


if __name__ == "__main__":
    main()
