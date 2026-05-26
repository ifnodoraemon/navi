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
            name="file.read",
            description="Read a UTF-8 text file inside the current project workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_bytes": {"type": "integer", "default": 200000},
                },
                "required": ["path"],
            },
            output_schema={"type": "object"},
        ),
        lambda args: _file_read(args, project_dir=registry.project_dir),
    )
    registry.register(
        ToolSpec(
            name="file.write",
            description="Write UTF-8 text to a file inside the current project workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "mode": {"type": "string", "default": "overwrite"},
                    "create_dirs": {"type": "boolean", "default": False},
                },
                "required": ["path", "content"],
            },
            output_schema={"type": "object"},
            facts_only=False,
            mutates=True,
            permission="write",
        ),
        lambda args: _file_write(args, project_dir=registry.project_dir),
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
    registry.register(
        ToolSpec(
            name="shell.run",
            description="Run a non-shell command in the project workspace and return bounded stdout/stderr facts.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string", "default": str(registry.project_dir)},
                    "timeout_seconds": {"type": "integer", "default": 20},
                },
                "required": ["command"],
            },
            output_schema={"type": "object"},
            facts_only=False,
            mutates=True,
            permission="write",
        ),
        lambda args: _shell_run(args, project_dir=registry.project_dir),
    )
    registry.register(
        ToolSpec(
            name="test.run",
            description="Run a project test command and return bounded stdout/stderr facts.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string", "default": str(registry.project_dir)},
                    "timeout_seconds": {"type": "integer", "default": 60},
                },
            },
            output_schema={"type": "object"},
            facts_only=False,
            mutates=True,
            permission="write",
        ),
        lambda args: _test_run(args, project_dir=registry.project_dir),
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
    listed_tasks = store.list(limit=limit)
    return ToolResult(
        tool="task.list",
        ok=True,
        facts={
            "tasks": [asdict(task) for task in listed_tasks],
            "watches": [asdict(watch) for watch in store.list_watches(limit=limit)],
            "task_status_counts": store.count_tasks_by_status(),
            "returned_task_count": len(listed_tasks),
            "task_limit": limit,
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
            ok=False,
            error=f"Failed to load config: {e}",
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
            "path": str(path),
            "mode": mode,
            "bytes_written": len(content.encode("utf-8")),
            "before_size": before_size,
            "after_size": after_size,
        },
    )


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


def _shell_run(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    command = _command_list(args.get("command"))
    if not command:
        return ToolResult(tool="shell.run", ok=False, error="command must be a non-empty string array")
    cwd, error = _project_path(args.get("cwd") or str(project_dir), project_dir=project_dir)
    if error:
        return ToolResult(tool="shell.run", ok=False, error=error)
    assert cwd is not None
    if not cwd.exists() or not cwd.is_dir():
        return ToolResult(tool="shell.run", ok=False, error="cwd must be an existing directory")
    timeout = _positive_int(args.get("timeout_seconds"), default=20, maximum=120)
    result = _run_command(command, cwd=cwd, timeout=timeout)
    return ToolResult(
        tool="shell.run",
        ok=result["exit_code"] == 0,
        facts={**result, "command": command, "cwd": str(cwd), "timeout_seconds": timeout},
        error=result["stderr"],
    )


def _test_run(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    command = _command_list(args.get("command")) or ["pytest", "-q"]
    cwd, error = _project_path(args.get("cwd") or str(project_dir), project_dir=project_dir)
    if error:
        return ToolResult(tool="test.run", ok=False, error=error)
    assert cwd is not None
    if not cwd.exists() or not cwd.is_dir():
        return ToolResult(tool="test.run", ok=False, error="cwd must be an existing directory")
    timeout = _positive_int(args.get("timeout_seconds"), default=60, maximum=300)
    result = _run_command(command, cwd=cwd, timeout=timeout)
    return ToolResult(
        tool="test.run",
        ok=result["exit_code"] == 0,
        facts={**result, "command": command, "cwd": str(cwd), "timeout_seconds": timeout},
        error=result["stderr"],
    )


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


def _run_command(command: list[str], *, cwd: Path, timeout: int) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "stdout": _truncate_output(exc.stdout or ""),
            "stderr": _truncate_output(exc.stderr or f"command timed out after {timeout} seconds"),
            "exit_code": 124,
            "timed_out": True,
        }
    except OSError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": 127, "timed_out": False}
    return {
        "stdout": _truncate_output(result.stdout),
        "stderr": _truncate_output(result.stderr),
        "exit_code": result.returncode,
        "timed_out": False,
    }


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


def _truncate_output(value: str, *, limit: int = 12000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


def _positive_int(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))
