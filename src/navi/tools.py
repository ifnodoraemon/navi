from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import load_config
from .fact_tools import service_facts, task_facts
from .weixin.store import WeixinStore


ToolHandler = Callable[[dict[str, Any]], "ToolResult"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    facts_only: bool = True
    mutates: bool = False
    permission: str = "read"
    source: str = "core"


@dataclass(frozen=True)
class ToolResult:
    tool: str
    ok: bool
    facts: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def duration_ms(self) -> int:
        if not self.started_at or not self.ended_at:
            return 0
        return int((self.ended_at - self.started_at) * 1000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "facts": self.facts,
            "error": self.error,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler


class ToolRegistry:
    def __init__(self, *, home: Path, project_dir: Path | None = None):
        self.home = home
        self.project_dir = project_dir or Path.cwd()
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = RegisteredTool(spec=spec, handler=handler)

    def list_specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in sorted(self._tools.values(), key=lambda item: item.spec.name)]

    def get(self, name: str) -> ToolSpec | None:
        tool = self._tools.get(name)
        return tool.spec if tool else None

    def call(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        tool = self._tools.get(name)
        started_at = time.time()
        if tool is None:
            return ToolResult(
                tool=name,
                ok=False,
                error=f"tool not found: {name}",
                started_at=started_at,
                ended_at=time.time(),
            )
        try:
            result = tool.handler(args or {})
        except Exception as exc:  # pragma: no cover - defensive boundary for plugins.
            return ToolResult(
                tool=name,
                ok=False,
                error=str(exc),
                started_at=started_at,
                ended_at=time.time(),
            )
        if result.started_at and result.ended_at:
            return result
        return ToolResult(
            tool=result.tool,
            ok=result.ok,
            facts=result.facts,
            error=result.error,
            started_at=started_at,
            ended_at=time.time(),
        )


def build_core_tool_registry(home: Path, *, project_dir: Path | None = None) -> ToolRegistry:
    registry = ToolRegistry(home=home, project_dir=project_dir)
    registry.register(
        ToolSpec(
            name="service.status",
            description="Return systemd user service facts.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "default": "navi.service"}},
            },
            output_schema={"type": "object"},
        ),
        lambda args: _service_status(args),
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
            name="provider.config",
            description="Return configured model provider facts without secrets.",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object"},
        ),
        lambda args: _provider_config(home),
    )
    registry.register(
        ToolSpec(
            name="connector.weixin.status",
            description="Return Weixin connector configuration facts without secrets.",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object"},
        ),
        lambda args: _weixin_status(home),
    )
    registry.register(
        ToolSpec(
            name="filesystem.list",
            description="Return directory entry facts.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "~"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
            output_schema={"type": "object"},
        ),
        lambda args: _filesystem_list(args),
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
        lambda args: _git_status(args, default_path=registry.project_dir),
    )
    return registry


def _service_status(args: dict[str, Any]) -> ToolResult:
    name = str(args.get("name") or "navi.service")
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
            "approvals": [asdict(approval) for approval in facts.approvals],
            "logs": [asdict(log) for log in facts.logs],
        },
        error="" if facts.task else "task not found",
    )


def _provider_config(home: Path) -> ToolResult:
    config = load_config(home)
    return ToolResult(
        tool="provider.config",
        ok=True,
        facts={
            "provider": config.model.provider,
            "model": config.model.model,
            "api_base_url": config.model.api_base_url,
            "has_api_key": bool(config.model.api_key),
        },
    )


def _weixin_status(home: Path) -> ToolResult:
    config = load_config(home)
    store = WeixinStore(home)
    status = {
        "configured": bool(config.weixin.account_id or store.list_accounts()),
        "account_id": config.weixin.account_id,
        "saved_accounts": store.list_accounts(),
        "dm_policy": config.weixin.dm_policy,
        "group_policy": config.weixin.group_policy,
    }
    return ToolResult(tool="connector.weixin.status", ok=True, facts=status)


def _filesystem_list(args: dict[str, Any]) -> ToolResult:
    raw_path = str(args.get("path") or "~")
    limit = _positive_int(args.get("limit"), default=50, maximum=200)
    path = Path(raw_path).expanduser()
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


def _git_status(args: dict[str, Any], *, default_path: Path) -> ToolResult:
    path = Path(str(args.get("path") or default_path)).expanduser()
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
