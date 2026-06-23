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
from .codebase import _command_list, _project_path
from .paths import _is_safe_path
from .run_command import _run_command, _run_git
from .utils import _positive_int

def _git_status(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    path = Path(str(args.get("path") or project_dir)).expanduser()
    if not _is_safe_path(path, project_dir):
        return ToolResult(
            tool="git.status", ok=False, error="path must be within the project directory"
        )

    branch = _run_git(path, "status", "--short", "--branch")
    root = _run_git(path, "rev-parse", "--show-toplevel")
    head = _run_git(path, "rev-parse", "--short", "HEAD")
    facts = {
        "path": str(path),
        "root": root["stdout"].strip(),
        "head": head["stdout"].strip(),
        "status": branch["stdout"].splitlines(),
        "exit_code": branch["exit_code"],
        "stderr": branch["stderr"],
    }
    return ToolResult(
        tool="git.status", ok=branch["exit_code"] == 0, facts=facts, error=branch["stderr"]
    )


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


def _test_run(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    command = _command_list(args.get("command"))
    if not command:
        return ToolResult(
            tool="test.run", ok=False, error="command must be a non-empty string array"
        )
    cwd, error = _project_path(args.get("cwd") or str(project_dir), project_dir=project_dir)
    if error:
        return ToolResult(tool="test.run", ok=False, error=error)
    assert cwd is not None
    if not cwd.exists() or not cwd.is_dir():
        return ToolResult(tool="test.run", ok=False, error="cwd must be an existing directory")
    timeout = _positive_int(args.get("timeout_seconds"), default=60, maximum=300)
    allocate_pty = bool(args.get("allocate_pty"))
    result = _run_command(command, cwd=cwd, timeout=timeout, allocate_pty=allocate_pty)
    return ToolResult(
        tool="test.run",
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


