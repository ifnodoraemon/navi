"""Core tool handlers."""
from __future__ import annotations
import os
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse
from typing import Any
from ..config import load_config
from ..fact_tools import service_facts, run_facts
from ..hooks import HookRegistry
from ..memory import MemoryStore
from ..operating_context import permission_allows
from ..runs import Approval
from ..safeguards import capability_safeguard_facts
from ..skills import SkillStore
from ..tools import ALL_EXECUTION_CONTEXTS, ToolRegistry, ToolResult, ToolSpec
from .codebase import _codebase_search, _project_path
from .paths import _is_safe_path
from .utils import _positive_int

def _directory_list(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    raw_path = str(args.get("path") or str(project_dir))
    limit = _positive_int(args.get("limit"), default=50, maximum=200)
    path = Path(raw_path).expanduser()

    if not _is_safe_path(path, project_dir):
        return ToolResult(
            tool="directory.list", ok=False, error="path must be within the project directory"
        )

    fact_path = path.resolve() if path.exists() else path
    facts: dict[str, Any] = {
        "path": str(fact_path),
        "exists": path.exists(),
        "is_dir": path.is_dir() if path.exists() else False,
        "entries": [],
        "limit": limit,
    }
    if not path.exists():
        return ToolResult(tool="directory.list", ok=False, facts=facts, error="path not found")
    if not path.is_dir():
        return ToolResult(
            tool="directory.list", ok=False, facts=facts, error="path is not a directory"
        )
    entries = []
    for child in sorted(path.iterdir(), key=lambda item: item.name)[:limit]:
        try:
            stat = child.stat()
            entries.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "type": "directory" if child.is_dir() else "file",
                    "size": stat.st_size,
                    "modified_at": stat.st_mtime,
                }
            )
        except OSError as exc:
            entries.append(
                {"name": child.name, "path": str(child), "type": "unknown", "error": str(exc)}
            )
    facts["entries"] = entries
    facts["entry_count"] = len(entries)
    return ToolResult(tool="directory.list", ok=True, facts=facts)


def _file_read(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    path, error = _project_path(args.get("path"), project_dir=project_dir)
    if error:
        return ToolResult(tool="file.read", ok=False, error=error)
    assert path is not None
    limit = _positive_int(args.get("max_bytes"), default=200000, maximum=1000000)
    facts = {"path": str(path), "max_bytes": limit, "truncated": False, "content": ""}
    if not path.exists():
        return ToolResult(tool="file.read", ok=False, facts=facts, error="path not found")
    if not path.is_file():
        return ToolResult(tool="file.read", ok=False, facts=facts, error="path is not a file")
    try:
        data = path.read_bytes()
    except OSError as exc:
        return ToolResult(tool="file.read", ok=False, facts=facts, error=str(exc))
    truncated = len(data) > limit
    chunk = data[:limit]
    facts.update(
        {
            "size": len(data),
            "truncated": truncated,
            "content": chunk.decode("utf-8", errors="replace"),
        }
    )
    return ToolResult(tool="file.read", ok=True, facts=facts)


def _file_write(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    path, error = _project_path(args.get("path"), project_dir=project_dir)
    if error:
        return ToolResult(tool="file.write", ok=False, error=error)
    assert path is not None
    content = str(args.get("content") or "")
    mode = str(args.get("mode") or "overwrite").strip().lower()
    if mode not in {"overwrite", "append"}:
        return ToolResult(tool="file.write", ok=False, error="mode must be overwrite or append")
    if path.exists() and path.is_dir():
        return ToolResult(tool="file.write", ok=False, error="path is a directory")
    parent = path.parent
    if not parent.exists():
        if bool(args.get("create_dirs")):
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return ToolResult(tool="file.write", ok=False, error=str(exc))
        else:
            return ToolResult(tool="file.write", ok=False, error="parent directory does not exist")
    try:
        before_size = path.stat().st_size if path.exists() else 0
        if mode == "append":
            with path.open("a", encoding="utf-8") as handle:
                handle.write(content)
        else:
            path.write_text(content, encoding="utf-8")
        after_size = path.stat().st_size
    except OSError as exc:
        return ToolResult(tool="file.write", ok=False, error=str(exc))
    return ToolResult(
        tool="file.write",
        ok=True,
        facts={
            "entity_type": "file",
            "entity_id": str(path),
            "state_transition": "appended" if mode == "append" else "written",
            "turn_scope": "current",
            "path": str(path),
            "mode": mode,
            "bytes_written": len(content.encode("utf-8")),
            "before_size": before_size,
            "after_size": after_size,
        },
    )


