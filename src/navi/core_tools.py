from __future__ import annotations

import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from .config import load_config
from .fact_tools import service_facts, run_facts
from .hooks import HookRegistry
from .memory import MemoryStore
from .operating_context import permission_allows
from .runs import Approval, RunStore
from .skills import SkillStore
from .tools import ToolRegistry, ToolResult, ToolSpec


def _output_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties}


def _array_of_objects() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "object"}}


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
            output_schema=_output_schema(
                {
                    "name": {"type": "string"},
                    "properties": {"type": "object"},
                    "exit_code": {"type": "integer"},
                    "stderr": {"type": "string"},
                }
            ),
        ),
        lambda args: _service_status(args, default_name=config.runtime.service_name),
    )
    registry.register(
        ToolSpec(
            name="delegate.status",
            description="Return delegation run, approval, and execution log facts.",
            input_schema={
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
            },
            output_schema=_output_schema(
                {
                    "run": {"type": "object"},
                    "approvals": _array_of_objects(),
                    "logs": _array_of_objects(),
                }
            ),
        ),
        lambda args: _run_status(home, args),
    )
    registry.register(
        ToolSpec(
            name="delegate.list",
            description="Return delegation runs and recurring watches as delegation-management facts.",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
            },
            output_schema=_output_schema(
                {
                    "runs": _array_of_objects(),
                    "watches": _array_of_objects(),
                    "run_status_counts": {"type": "object"},
                    "returned_run_count": {"type": "integer"},
                    "run_limit": {"type": "integer"},
                }
            ),
        ),
        lambda args: _run_list(home, args),
    )
    registry.register(
        ToolSpec(
            name="provider.config",
            description="Return configured model provider facts without secrets.",
            input_schema={"type": "object", "properties": {}},
            output_schema=_output_schema(
                {
                    "provider": {"type": "string"},
                    "kind": {"type": "string"},
                    "model": {"type": "string"},
                    "api_base_url": {"type": "string"},
                    "has_api_key": {"type": "boolean"},
                    "fallbacks": _array_of_objects(),
                    "routes": {"type": "object"},
                    "validation_errors": {"type": "array", "items": {"type": "string"}},
                }
            ),
        ),
        lambda args: _provider_config(home),
    )
    registry.register(
        ToolSpec(
            name="skills.list",
            description="Return installed procedural skill facts.",
            input_schema={"type": "object", "properties": {}},
            output_schema=_output_schema(
                {
                    "category": {"type": "string"},
                    "definition": {"type": "string"},
                    "not_tools": {"type": "boolean"},
                    "prompt_permission_ceiling": {"type": "string"},
                    "skills": _array_of_objects(),
                    "count": {"type": "integer"},
                }
            ),
        ),
        lambda args: _skills_list(home, workspace=registry.project_dir),
    )
    registry.register(
        ToolSpec(
            name="skills.view",
            description="Return one installed skill's full instructions or a safe supporting file by skill name.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "relative_path": {"type": "string", "default": "SKILL.md"},
                    "max_bytes": {"type": "integer", "default": 50000},
                },
                "required": ["name"],
            },
            output_schema=_output_schema(
                {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "permission": {"type": "string"},
                    "injectable_with_read_ceiling": {"type": "boolean"},
                    "path": {"type": "string"},
                    "relative_path": {"type": "string"},
                    "size": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                    "content": {"type": "string"},
                }
            ),
        ),
        lambda args: _skills_view(home, args, workspace=registry.project_dir),
    )
    registry.register(
        ToolSpec(
            name="tools.list",
            description="Return callable capability facts.",
            input_schema={"type": "object", "properties": {}},
            output_schema=_output_schema(
                {
                    "category": {"type": "string"},
                    "definition": {"type": "string"},
                    "not_skills": {"type": "boolean"},
                    "tools": _array_of_objects(),
                    "count": {"type": "integer"},
                }
            ),
        ),
        lambda args: _tools_list(registry),
    )
    registry.register(
        ToolSpec(
            name="hooks.list",
            description="Return lifecycle hook facts.",
            input_schema={"type": "object", "properties": {}},
            output_schema=_output_schema(
                {
                    "category": {"type": "string"},
                    "definition": {"type": "string"},
                    "hooks": _array_of_objects(),
                    "count": {"type": "integer"},
                }
            ),
        ),
        lambda args: _hooks_list(home),
    )
    registry.register(
        ToolSpec(
            name="memory.list",
            description="Return typed memory item facts from Navi's local memory store.",
            input_schema={
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "status": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
            },
            output_schema=_output_schema(
                {
                    "items": _array_of_objects(),
                    "count": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "type": {"type": "string"},
                    "status": {"type": "string"},
                }
            ),
        ),
        lambda args: _memory_list(home, args),
    )
    registry.register(
        ToolSpec(
            name="memory.recall",
            description="Recall goal-relevant memory facts from Navi's local memory store.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
            output_schema=_output_schema(
                {
                    "query": {"type": "string"},
                    "items": _array_of_objects(),
                    "count": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "rendered": {"type": "string"},
                }
            ),
        ),
        lambda args: _memory_recall(home, args),
    )
    registry.register(
        ToolSpec(
            name="browser.screenshot",
            description="Capture a browser screenshot artifact for a reachable page.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "path": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "default": 30},
                },
                "required": ["url", "path"],
            },
            output_schema=_output_schema(
                {
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "timed_out": {"type": "boolean"},
                    "url": {"type": "string"},
                    "path": {"type": "string"},
                    "exists": {"type": "boolean"},
                    "size": {"type": "integer"},
                }
            ),
            facts_only=True,
            mutates=True,
            permission="write",
        ),
        lambda args: _browser_screenshot(args, project_dir=registry.project_dir),
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
            output_schema=_output_schema(
                {
                    "path": {"type": "string"},
                    "exists": {"type": "boolean"},
                    "is_dir": {"type": "boolean"},
                    "is_file": {"type": "boolean"},
                    "size": {"type": "integer"},
                    "entries": _array_of_objects(),
                    "entry_count": {"type": "integer"},
                    "limit": {"type": "integer"},
                }
            ),
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
            output_schema=_output_schema(
                {
                    "path": {"type": "string"},
                    "max_bytes": {"type": "integer"},
                    "size": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                    "content": {"type": "string"},
                }
            ),
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
            output_schema=_output_schema(
                {
                    "path": {"type": "string"},
                    "mode": {"type": "string"},
                    "bytes_written": {"type": "integer"},
                    "before_size": {"type": "integer"},
                    "after_size": {"type": "integer"},
                }
            ),
            facts_only=True,
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
            output_schema=_output_schema(
                {
                    "path": {"type": "string"},
                    "root": {"type": "string"},
                    "head": {"type": "string"},
                    "status": {"type": "array", "items": {"type": "string"}},
                    "exit_code": {"type": "integer"},
                    "stderr": {"type": "string"},
                }
            ),
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
            output_schema=_output_schema(
                {
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "timed_out": {"type": "boolean"},
                    "command": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string"},
                    "timeout_seconds": {"type": "integer"},
                }
            ),
            facts_only=True,
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
            output_schema=_output_schema(
                {
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "timed_out": {"type": "boolean"},
                    "command": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string"},
                    "timeout_seconds": {"type": "integer"},
                }
            ),
            facts_only=True,
            mutates=True,
            permission="write",
        ),
        lambda args: _test_run(args, project_dir=registry.project_dir),
    )


def _service_status(args: dict[str, Any], *, default_name: str) -> ToolResult:
    name = str(args.get("name") or default_name)
    facts = service_facts(name)
    return ToolResult(tool="service.status", ok=facts.exit_code == 0, facts=asdict(facts), error=facts.stderr)


def _run_status(home: Path, args: dict[str, Any]) -> ToolResult:
    run_id = args.get("run_id")
    facts = run_facts(home, str(run_id) if run_id else None)
    return ToolResult(
        tool="delegate.status",
        ok=facts.run is not None,
        facts={
            "run": asdict(facts.run) if facts.run else None,
            "approvals": [_approval_facts(approval) for approval in facts.approvals],
            "logs": [asdict(log) for log in facts.logs],
        },
        error="" if facts.run else "delegation run not found",
    )


def _run_list(home: Path, args: dict[str, Any]) -> ToolResult:
    limit = _positive_int(args.get("limit"), default=20, maximum=100)
    store = RunStore(home)
    listed_runs = store.list(limit=limit)
    return ToolResult(
        tool="delegate.list",
        ok=True,
        facts={
            "runs": [asdict(run) for run in listed_runs],
            "watches": [asdict(watch) for watch in store.list_watches(limit=limit)],
            "run_status_counts": store.count_runs_by_status(),
            "returned_run_count": len(listed_runs),
            "run_limit": limit,
        },
    )


def _approval_facts(approval: Approval) -> dict[str, Any]:
    return {
        "id": approval.id,
        "run_id": approval.run_id,
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


def _skills_list(home: Path, *, workspace: Path) -> ToolResult:
    skills = SkillStore(home).list_skills(permission_ceiling="write", workspace=workspace)
    return ToolResult(
        tool="skills.list",
        ok=True,
        facts={
            "category": "skills",
            "definition": "procedural guidance packages loaded into Navi's prompt context",
            "not_tools": True,
            "prompt_permission_ceiling": "read",
            "skills": [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "source": skill.source,
                    "scope": skill.scope,
                    "permission": skill.permission,
                    "injectable_with_read_ceiling": permission_allows(skill.permission, "read"),
                    "verified": skill.verified,
                    "tags": list(skill.tags),
                }
                for skill in skills
            ],
            "count": len(skills),
        },
    )


def _skills_view(home: Path, args: dict[str, Any], *, workspace: Path) -> ToolResult:
    name = str(args.get("name") or "").strip().lower()
    if not name:
        return ToolResult(tool="skills.view", ok=False, error="name is required")
    relative = str(args.get("relative_path") or "SKILL.md").strip() or "SKILL.md"
    limit = _positive_int(args.get("max_bytes"), default=50000, maximum=200000)
    store = SkillStore(home)
    skills = store.list_skills(permission_ceiling="write", workspace=workspace)
    skill = next((item for item in skills if item.name.lower() == name or item.path.parent.name.lower() == name), None)
    if skill is None:
        return ToolResult(tool="skills.view", ok=False, error="skill not found", facts={"name": name})
    base_dir = skill.path.parent.resolve()
    target = (base_dir / relative).resolve()
    if base_dir != target and base_dir not in target.parents:
        return ToolResult(tool="skills.view", ok=False, error="relative_path must stay inside the skill directory")
    if not target.exists() or not target.is_file():
        return ToolResult(tool="skills.view", ok=False, error="skill file not found", facts={"path": str(target)})
    data = target.read_bytes()
    truncated = len(data) > limit
    content = data[:limit].decode("utf-8", errors="replace")
    return ToolResult(
        tool="skills.view",
        ok=True,
        facts={
            "name": skill.name,
            "description": skill.description,
            "permission": skill.permission,
            "injectable_with_read_ceiling": permission_allows(skill.permission, "read"),
            "path": str(target),
            "relative_path": str(target.relative_to(base_dir)),
            "size": len(data),
            "truncated": truncated,
            "content": content,
        },
    )


def _tools_list(registry: ToolRegistry) -> ToolResult:
    specs = registry.list_specs()
    return ToolResult(
        tool="tools.list",
        ok=True,
        facts={
            "category": "tools",
            "definition": "callable gateway tools registered in this gateway context",
            "not_skills": True,
            "tools": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "permission": spec.permission,
                    "facts_only": spec.facts_only,
                    "mutates": spec.mutates,
                    "source": spec.source,
                    "input_properties": sorted((spec.input_schema.get("properties") or {}).keys()),
                    "required": list(spec.input_schema.get("required") or []),
                }
                for spec in specs
            ],
            "count": len(specs),
        },
    )


def _hooks_list(home: Path) -> ToolResult:
    return ToolResult(tool="hooks.list", ok=True, facts=HookRegistry(home).list_facts())


def _memory_item_facts(item) -> dict[str, Any]:
    return {
        "id": item.id,
        "type": item.type,
        "status": item.status,
        "scope": item.scope,
        "content": item.content,
        "source": item.source,
        "confidence": item.confidence,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "last_verified_at": item.last_verified_at,
        "expires_at": item.expires_at,
        "metadata": item.metadata,
    }


def _memory_recall_facts(recall) -> dict[str, Any]:
    facts = _memory_item_facts(recall.item)
    facts["score"] = recall.score
    facts["reasons"] = list(recall.reasons)
    return facts


def _memory_list(home: Path, args: dict[str, Any]) -> ToolResult:
    limit = _positive_int(args.get("limit"), default=20, maximum=100)
    memory_type = str(args.get("type") or "").strip().lower() or None
    status = str(args.get("status") or "").strip().lower() or None
    try:
        items = MemoryStore(home).list_items(memory_type=memory_type, status=status, limit=limit)
    except ValueError as exc:
        return ToolResult(tool="memory.list", ok=False, error=str(exc))
    return ToolResult(
        tool="memory.list",
        ok=True,
        facts={
            "items": [_memory_item_facts(item) for item in items],
            "count": len(items),
            "limit": limit,
            "type": memory_type or "",
            "status": status or "",
        },
    )


def _memory_recall(home: Path, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(tool="memory.recall", ok=False, error="query is required")
    limit = _positive_int(args.get("limit"), default=8, maximum=50)
    store = MemoryStore(home)
    recalls = store.recall(query, limit=limit)
    return ToolResult(
        tool="memory.recall",
        ok=True,
        facts={
            "query": query,
            "items": [_memory_recall_facts(recall) for recall in recalls],
            "count": len(recalls),
            "limit": limit,
            "rendered": store.render_context(query, limit=limit),
        },
    )


def _browser_screenshot(args: dict[str, Any], *, project_dir: Path) -> ToolResult:
    url = str(args.get("url") or "").strip()
    if not _is_browser_url(url):
        return ToolResult(tool="browser.screenshot", ok=False, error="url must be http(s) or localhost")
    output, error = _project_path(args.get("path"), project_dir=project_dir)
    if error:
        return ToolResult(tool="browser.screenshot", ok=False, error=error)
    assert output is not None
    if output.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        return ToolResult(tool="browser.screenshot", ok=False, error="path must end with .png, .jpg, or .jpeg")
    playwright = shutil.which("playwright")
    if not playwright:
        return ToolResult(
            tool="browser.screenshot",
            ok=False,
            error="playwright CLI not found",
            facts={"url": url, "path": str(output)},
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    timeout = _positive_int(args.get("timeout_seconds"), default=30, maximum=120)
    result = _run_command([playwright, "screenshot", url, str(output)], cwd=project_dir, timeout=timeout)
    ok = result["exit_code"] == 0 and output.exists()
    return ToolResult(
        tool="browser.screenshot",
        ok=ok,
        error="" if ok else result["stderr"],
        facts={
            **result,
            "url": url,
            "path": str(output),
            "exists": output.exists(),
            "size": output.stat().st_size if output.exists() else 0,
        },
    )


def _is_safe_path(path: Path, project_dir: Path) -> bool:
    try:
        resolved_path = path.resolve().absolute()
        resolved_project = project_dir.resolve().absolute()
        return resolved_project == resolved_path or resolved_project in resolved_path.parents
    except Exception:
        return False


def _is_browser_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    return bool(host)


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
