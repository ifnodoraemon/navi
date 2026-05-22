from __future__ import annotations

import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import load_config
from .fact_tools import service_facts, task_facts
from .tasks import Approval, TaskStore
from .tools import ToolRegistry, ToolResult, ToolSpec


def register_core_tools(registry: ToolRegistry, *, home: Path) -> None:
    config = load_config(home)
    registry.register(
        ToolSpec(
            name="service.status",
            description="Return systemd user service facts.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "default": config.runtime.service_name}},
            },
            output_schema={"type": "object"},
        ),
        lambda args: _service_status(args, default_name=config.runtime.service_name),
    )
    registry.register(
        ToolSpec(
            name="task.status",
            description="Return task, approval, and execution log facts.",
            input_schema={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
            },
            output_schema={"type": "object"},
        ),
        lambda args: _task_status(home, args),
    )
    registry.register(
        ToolSpec(
            name="task.list",
            description="Return tracked tasks and recurring watches as task-management facts.",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
            },
            output_schema={"type": "object"},
        ),
        lambda args: _task_list(home, args),
    )
    registry.register(
        ToolSpec(
            name="provider.config",
            description="Return configured model provider facts without secrets.",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object"},
        ),
        lambda args: _provider_config(home),
    )
    registry.register(
        ToolSpec(
            name="filesystem.list",
            description="Return directory entry facts.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": str(registry.project_dir)},
                    "limit": {"type": "integer", "default": 50},
                },
            },
            output_schema={"type": "object"},
        ),
        lambda args: _filesystem_list(args, project_dir=registry.project_dir),
    )
    registry.register(
        ToolSpec(
            name="git.status",
            description="Return git repository status facts.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "default": str(registry.project_dir)}},
            },
            output_schema={"type": "object"},
        ),
        lambda args: _git_status(args, project_dir=registry.project_dir),
    )


def _service_status(args: dict[str, Any], *, default_name: str) -> ToolResult:
    name = str(args.get("name") or default_name)
    facts = service_facts(name)
    return ToolResult(tool="service.status", ok=facts.exit_code == 0, facts=asdict(facts), error=facts.stderr)


def _task_status(home: Path, args: dict[str, Any]) -> ToolResult:
    task_id = args.get("task_id")
    facts = task_facts(home, str(task_id) if task_id else None)
    return ToolResult(
        tool="task.status",
        ok=facts.task is not None,
        facts={
            "task": asdict(facts.task) if facts.task else None,
            "approvals": [_approval_facts(approval) for approval in facts.approvals],
            "logs": [asdict(log) for log in facts.logs],
        },
        error="" if facts.task else "task not found",
    )


def _task_list(home: Path, args: dict[str, Any]) -> ToolResult:
    limit = _positive_int(args.get("limit"), default=20, maximum=100)
    store = TaskStore(home)
    return ToolResult(
        tool="task.list",
        ok=True,
        facts={
            "tasks": [asdict(task) for task in store.list(limit=limit)],
            "watches": [asdict(watch) for watch in store.list_watches(limit=limit)],
        },
    )


def _approval_facts(approval: Approval) -> dict[str, Any]:
    return {
        "id": approval.id,
        "task_id": approval.task_id,
        "action": approval.action,
        "peer_id": approval.peer_id,
        "sender_id": approval.sender_id,
        "status": approval.status,
        "expires_at": approval.expires_at,
        "created_at": approval.created_at,
        "updated_at": approval.updated_at,
        "code_present": bool(approval.code),
    }


def _provider_config(home: Path) -> ToolResult:
    from .config import validate_config, load_config
    try:
        config = load_config(home)
        errors = validate_config(config, home)
        return ToolResult(
            tool="provider.config",
            ok=True,
            facts={
                "provider": config.model.provider,
                "kind": config.model.kind,
                "model": config.model.model,
                "api_base_url": config.model.api_base_url,
                "has_api_key": bool(config.model.api_key),
                "fallbacks": [
                    {
                        "provider": item.provider,
                        "kind": item.kind,
                        "model": item.model,
                        "api_base_url": item.api_base_url,
                        "has_api_key": bool(item.api_key),
                    }
                    for item in config.model.fallbacks
                ],
                "routes": {
                    role: {
                        "provider": item.provider,
                        "kind": item.kind,
                        "model": item.model,
                        "api_base_url": item.api_base_url,
                        "has_api_key": bool(item.api_key),
                        "fallback_count": len(item.fallbacks),
                    }
                    for role, item in config.model.routes.items()
                },
                "validation_errors": errors,
            },
        )
    except Exception as e:
        return ToolResult(
            tool="provider.config",
            ok=True,
            facts={
                "provider": "",
                "kind": "",
                "model": "",
                "api_base_url": "",
                "has_api_key": False,
                "fallbacks": [],
                "routes": {},
                "validation_errors": [f"Failed to load config: {e}"],
            },
        )


def _is_safe_path(path: Path, project_dir: Path) -> bool:
    try:
        resolved_path = path.resolve().absolute()
        resolved_project = project_dir.resolve().absolute()
        return resolved_project == resolved_path or resolved_project in resolved_path.parents
    except Exception:
        return False


def _filesystem_list(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    raw_path = str(args.get("path") or str(project_dir))
    limit = _positive_int(args.get("limit"), default=50, maximum=200)
    path = Path(raw_path).expanduser()

    if not _is_safe_path(path, project_dir):
        return ToolResult(tool="filesystem.list", ok=False, error="path must be within the project directory")

    fact_path = path.resolve() if path.exists() else path
    facts: dict[str, Any] = {
        "path": str(fact_path),
        "exists": path.exists(),
        "is_dir": path.is_dir() if path.exists() else False,
        "entries": [],
        "limit": limit,
    }
    if not path.exists():
        return ToolResult(tool="filesystem.list", ok=False, facts=facts, error="path not found")
    if not path.is_dir():
        facts["is_file"] = path.is_file()
        facts["size"] = path.stat().st_size
        return ToolResult(tool="filesystem.list", ok=False, facts=facts, error="path is not a directory")
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
            entries.append({"name": child.name, "path": str(child), "type": "unknown", "error": str(exc)})
    facts["entries"] = entries
    facts["entry_count"] = len(entries)
    return ToolResult(tool="filesystem.list", ok=True, facts=facts)


def _git_status(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    path = Path(str(args.get("path") or project_dir)).expanduser()
    if not _is_safe_path(path, project_dir):
        return ToolResult(tool="git.status", ok=False, error="path must be within the project directory")

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
    return ToolResult(tool="git.status", ok=branch["exit_code"] == 0, facts=facts, error=branch["stderr"])


def _run_git(path: Path, *args: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=path,
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except OSError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": 127}
    return {"stdout": result.stdout, "stderr": result.stderr.strip(), "exit_code": result.returncode}


def _positive_int(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))
