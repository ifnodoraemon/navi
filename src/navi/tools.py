from __future__ import annotations

import time
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .connector_registry import load_connector_adapters
from .tasks import TaskStore


logger = logging.getLogger(__name__)
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


ToolProviderRegister = Callable[["ToolRegistry"], None]


@dataclass(frozen=True)
class ToolProvider:
    name: str
    source: str
    register: ToolProviderRegister


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

    def list_sources(self) -> list[str]:
        return sorted({tool.spec.source for tool in self._tools.values()})

    def registered_tools(self) -> list[RegisteredTool]:
        return [tool for tool in sorted(self._tools.values(), key=lambda item: item.spec.name)]

    def get(self, name: str) -> ToolSpec | None:
        tool = self._tools.get(name)
        return tool.spec if tool else None

    def call(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        tool = self._tools.get(name)
        started_at = time.time()
        if tool is None:
            result = ToolResult(
                tool=name,
                ok=False,
                error=f"tool not found: {name}",
                started_at=started_at,
                ended_at=time.time(),
            )
            self._audit_call(args or {}, result)
            return result
        try:
            result = tool.handler(args or {})
        except Exception as exc:  # pragma: no cover - defensive boundary for plugins.
            result = ToolResult(
                tool=name,
                ok=False,
                error=str(exc),
                started_at=started_at,
                ended_at=time.time(),
            )
            self._audit_call(args or {}, result)
            return result
        if result.started_at and result.ended_at:
            self._audit_call(args or {}, result)
            return result
        result = ToolResult(
            tool=result.tool,
            ok=result.ok,
            facts=result.facts,
            error=result.error,
            started_at=started_at,
            ended_at=time.time(),
        )
        self._audit_call(args or {}, result)
        return result

    def _audit_call(self, args: dict[str, Any], result: ToolResult) -> None:
        try:
            TaskStore(self.home).add_tool_call_log(
                tool=result.tool,
                args_json=json.dumps(args, ensure_ascii=False, sort_keys=True),
                ok=result.ok,
                facts_json=json.dumps(result.facts, ensure_ascii=False, sort_keys=True),
                error=result.error,
                started_at=result.started_at,
                ended_at=result.ended_at,
            )
        except Exception:
            logger.exception("failed to audit tool call: %s", result.tool)


class ToolGateway:
    def __init__(
        self,
        *,
        home: Path,
        project_dir: Path | None = None,
        providers: list[ToolProvider] | None = None,
        allow_sources: set[str] | None = None,
        disabled_tools: set[str] | None = None,
        permission_ceiling: str = "write",
    ):
        self.home = home
        self.project_dir = project_dir or Path.cwd()
        self.providers = providers or load_tool_providers(home, project_dir=self.project_dir)
        self.allow_sources = allow_sources
        self.disabled_tools = disabled_tools or set()
        self.permission_ceiling = permission_ceiling
        self.registry = ToolRegistry(home=home, project_dir=self.project_dir)
        self.refresh()

    def refresh(self) -> None:
        raw = ToolRegistry(home=self.home, project_dir=self.project_dir)
        for provider in self.providers:
            provider.register(raw)
        self.registry = ToolRegistry(home=self.home, project_dir=self.project_dir)
        for tool in raw.registered_tools():
            if self.allow_sources is not None and tool.spec.source not in self.allow_sources:
                continue
            if tool.spec.name in self.disabled_tools:
                continue
            if not _permission_allows(tool.spec.permission, self.permission_ceiling):
                continue
            self.registry.register(tool.spec, tool.handler)

    def list_specs(self) -> list[ToolSpec]:
        return self.registry.list_specs()

    def list_sources(self) -> list[str]:
        return self.registry.list_sources()

    def get(self, name: str) -> ToolSpec | None:
        return self.registry.get(name)

    def call(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        return self.registry.call(name, args)


def load_tool_providers(home: Path, *, project_dir: Path | None = None) -> list[ToolProvider]:
    return [
        ToolProvider(
            name="core-facts",
            source="core",
            register=lambda registry: _register_core_fact_tools(registry, home=home),
        ),
        ToolProvider(
            name="connectors",
            source="connectors",
            register=lambda registry: _register_connector_tools(registry, home=home),
        ),
    ]


def build_tool_gateway(
    home: Path,
    *,
    project_dir: Path | None = None,
    allow_sources: set[str] | None = None,
    disabled_tools: set[str] | None = None,
    permission_ceiling: str = "write",
) -> ToolGateway:
    return ToolGateway(
        home=home,
        project_dir=project_dir,
        allow_sources=allow_sources,
        disabled_tools=disabled_tools,
        permission_ceiling=permission_ceiling,
    )


def _register_core_fact_tools(registry: ToolRegistry, *, home: Path) -> None:
    from .core_tools import register_core_tools

    register_core_tools(registry, home=home)


def _register_connector_tools(registry: ToolRegistry, *, home: Path) -> None:
    for adapter in load_connector_adapters():
        adapter.register_tools(registry, home)


def _permission_allows(required: str, ceiling: str) -> bool:
    order = {"read": 0, "prepare": 1, "write": 2}
    return order.get(required, 0) <= order.get(ceiling, 0)
