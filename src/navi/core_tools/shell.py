"""Core tool handlers."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from ..tools import ToolResult
from .codebase import _command_list, _project_path
from .run_command import _normalize_argv, _run_command
from .utils import _positive_int


def _shell_run(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    command = _command_list(args.get("command"))
    if not command:
        return ToolResult(
            tool="shell.run", ok=False, error="command must be a non-empty string array"
        )
    cwd, error = _project_path(args.get("cwd") or str(project_dir), project_dir=project_dir)
    if error:
        return ToolResult(tool="shell.run", ok=False, error=error)
    assert cwd is not None
    if not cwd.exists() or not cwd.is_dir():
        return ToolResult(tool="shell.run", ok=False, error="cwd must be an existing directory")
    timeout = _positive_int(args.get("timeout_seconds"), default=20, maximum=120)
    allocate_pty = bool(args.get("allocate_pty"))
    command = _normalize_argv(command)
    result = _run_command(command, cwd=cwd, timeout=timeout, allocate_pty=allocate_pty)
    return ToolResult(
        tool="shell.run",
        ok=result["exit_code"] == 0,
        facts={
            "entity_type": "process",
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
