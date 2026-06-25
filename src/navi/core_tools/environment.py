"""Local environment fact tool handlers."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..tools import ToolResult
from .codebase import _command_list, _project_path
from .run_command import _run_command, _run_git
from .utils import _positive_int


def _directory_list(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    path, error = _project_path(args.get("path") or ".", project_dir=project_dir)
    if error:
        return ToolResult(tool="directory.list", ok=False, error=error)
    assert path is not None
    limit = _positive_int(args.get("limit"), default=100, maximum=500)
    include_hidden = bool(args.get("include_hidden"))
    if not path.exists():
        return ToolResult(tool="directory.list", ok=False, error="path not found")
    if not path.is_dir():
        return ToolResult(tool="directory.list", ok=False, error="path is not a directory")
    try:
        entries = []
        for item in sorted(path.iterdir(), key=lambda entry: entry.name.lower()):
            if not include_hidden and item.name.startswith("."):
                continue
            stat = item.stat()
            entries.append(
                {
                    "name": item.name,
                    "path": str(item),
                    "type": "directory" if item.is_dir() else "file",
                    "size": stat.st_size,
                    "modified_at": stat.st_mtime,
                }
            )
            if len(entries) >= limit:
                break
    except OSError as exc:
        return ToolResult(tool="directory.list", ok=False, error=str(exc))
    return ToolResult(
        tool="directory.list",
        ok=True,
        facts={
            "path": str(path),
            "entries": entries,
            "count": len(entries),
            "limit": limit,
            "include_hidden": include_hidden,
        },
    )


def _git_status(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    path, error = _project_path(args.get("path") or ".", project_dir=project_dir)
    if error:
        return ToolResult(tool="git.status", ok=False, error=error)
    assert path is not None
    cwd = path if path.is_dir() else path.parent
    status = _run_git(cwd, "status", "--short", "--branch")
    branch = ""
    if status["stdout"]:
        first = status["stdout"].splitlines()[0]
        if first.startswith("## "):
            branch = first[3:]
    return ToolResult(
        tool="git.status",
        ok=status["exit_code"] == 0,
        facts={
            "path": str(cwd),
            "branch": branch,
            "stdout": status["stdout"],
            "stderr": status["stderr"],
            "exit_code": status["exit_code"],
        },
        error=status["stderr"],
    )


def _system_info(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    del args
    disk = shutil.disk_usage(project_dir)
    return ToolResult(
        tool="system.info",
        ok=True,
        facts={
            "platform": platform.platform(),
            "python_version": sys.version.split()[0],
            "cpu_count": os.cpu_count(),
            "project_dir": str(project_dir),
            "disk": {
                "total": disk.total,
                "used": disk.used,
                "free": disk.free,
            },
        },
    )


def _service_status(args: dict[str, Any]) -> ToolResult:
    name = str(args.get("name") or "navi.service").strip()
    if not name:
        return ToolResult(tool="service.status", ok=False, error="name is required")
    command = [
        "systemctl",
        "--user",
        "show",
        name,
        "--no-pager",
        "--property=Id,LoadState,ActiveState,SubState,ExecMainStartTimestamp",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except OSError as exc:
        return ToolResult(tool="service.status", ok=False, error=str(exc), facts={"name": name})
    except subprocess.TimeoutExpired as exc:
        return ToolResult(
            tool="service.status",
            ok=False,
            error=f"service status timed out: {exc}",
            facts={"name": name, "timed_out": True},
        )
    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        properties[key] = value
    return ToolResult(
        tool="service.status",
        ok=result.returncode == 0,
        facts={
            "name": name,
            "properties": properties,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        },
        error=result.stderr,
    )


def _test_run(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    command = _command_list(args.get("command")) or [sys.executable, "-m", "pytest", "-q"]
    cwd, error = _project_path(args.get("cwd") or ".", project_dir=project_dir)
    if error:
        return ToolResult(tool="test.run", ok=False, error=error)
    assert cwd is not None
    if not cwd.exists() or not cwd.is_dir():
        return ToolResult(tool="test.run", ok=False, error="cwd must be an existing directory")
    timeout = _positive_int(args.get("timeout_seconds"), default=120, maximum=600)
    result = _run_command(command, cwd=cwd, timeout=timeout)
    return ToolResult(
        tool="test.run",
        ok=result["exit_code"] == 0,
        facts={
            "entity_type": "test_run",
            "entity_id": " ".join(command),
            "state_transition": "executed",
            "turn_scope": "current",
            **result,
            "command": command,
            "cwd": str(cwd),
            "timeout_seconds": timeout,
        },
        error=result["stderr"],
    )
