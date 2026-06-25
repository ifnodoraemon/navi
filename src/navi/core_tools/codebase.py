"""Core tool handlers."""
from __future__ import annotations
import os
import shutil
from pathlib import Path
from typing import Any
from ..tools import ToolResult
from .paths import _is_safe_path

def _project_path(value: Any, *, project_dir: Path) -> tuple[Path | None, str]:
    raw = str(value or "").strip()
    if not raw:
        return None, "path is required"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = project_dir / path
    if not _is_safe_path(path, project_dir):
        return None, "path must be within the project directory"
    return path.resolve().absolute(), ""


def _command_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    command = [str(item) for item in value if isinstance(item, str) and item]
    return command[:32]


def _codebase_search(args: dict[str, Any], *, project_dir: Path, home: Path) -> ToolResult:
    query = str(args.get("query") or "")
    limit = int(args.get("limit") or 5)
    if not query:
        return ToolResult(tool="codebase.search", ok=False, error="query is required")

    try:
        from ..rag import CodebaseRAG

        rag = CodebaseRAG(project_dir, db_path=home / "codebase_rag.db")
        results = rag.search(query, limit=limit)
        return ToolResult(
            tool="codebase.search",
            ok=True,
            facts={
                "results": [{"path": r.path, "snippet": r.content, "rank": r.rank} for r in results]
            },
        )
    except Exception as exc:
        return ToolResult(tool="codebase.search", ok=False, error=str(exc))


def _resolve_binary_error(command: list[str]) -> str:
    """Pre-flight check: return an error message if ``command[0]`` is not on
    PATH. Without this guard a missing binary raises a confusing
    ``[Errno 2] No such file or directory: 'python'`` that the planner
    blindly retries. We surface a structured hint instead (e.g. use
    ``python3`` when only ``python3`` exists)."""
    if not command:
        return ""
    binary = command[0]
    if not binary:
        return ""
    # Absolute or relative path: caller owns resolution, don't second-guess.
    if (
        "/" in binary
        or "\\" in binary
        or os.path.sep in binary
        or os.path.altsep and os.path.altsep in binary
    ):
        return ""
    if shutil.which(binary):
        return ""
    # Binary not found - suggest known alternatives for the common cases.
    hints = {
        "python": "python3",
        "python3": "python",
        "pytest": "python3 -m pytest",
    }
    suggestion = hints.get(binary)
    if suggestion:
        suggested_bin = suggestion.split()[0]
        if shutil.which(suggested_bin):
            return (
                f"binary '{binary}' not found on PATH. "
                f"Try '{suggestion}' instead."
            )
    return f"binary '{binary}' not found on PATH."

