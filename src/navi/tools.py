from __future__ import annotations

import time
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .connector_registry import load_connector_adapters
from .runs import RunStore


logger = logging.getLogger(__name__)
ToolHandler = Callable[[dict[str, Any]], "ToolResult"]


def validate_schema(data: Any, schema: dict[str, Any], path: str = "") -> list[str]:
    """Validates data against a simplified JSON Schema. Returns list of error messages."""
    errors = []
    if not isinstance(schema, dict):
        return errors

    expected_type = schema.get("type")

    if expected_type == "object":
        if not isinstance(data, dict):
            errors.append(f"'{path or 'input'}' must be an object, got {type(data).__name__}")
            return errors

        # Check required fields
        required = schema.get("required", [])
        for field in required:
            if field not in data:
                errors.append(f"'{path or 'input'}' is missing required property: {field}")

        # Validate properties
        properties = schema.get("properties", {})
        for key, val in data.items():
            if key in properties:
                errors.extend(
                    validate_schema(val, properties[key], f"{path}.{key}" if path else key)
                )

    elif expected_type == "array":
        if not isinstance(data, (list, tuple)):
            errors.append(f"'{path or 'input'}' must be an array, got {type(data).__name__}")
            return errors
        items = schema.get("items")
        if items:
            for idx, item in enumerate(data):
                errors.extend(validate_schema(item, items, f"{path}[{idx}]"))

    elif expected_type == "string":
        if not isinstance(data, str):
            errors.append(f"'{path or 'input'}' must be a string, got {type(data).__name__}")

    elif expected_type == "integer":
        if not isinstance(data, int) or isinstance(data, bool):
            errors.append(f"'{path or 'input'}' must be an integer, got {type(data).__name__}")

    elif expected_type == "number":
        if not isinstance(data, (int, float)) or isinstance(data, bool):
            errors.append(f"'{path or 'input'}' must be a number, got {type(data).__name__}")

    elif expected_type == "boolean":
        if not isinstance(data, bool):
            errors.append(f"'{path or 'input'}' must be a boolean, got {type(data).__name__}")

    return errors


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
    routing_hints: list[str] = field(default_factory=list)


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
    def __init__(self, *, home: Path, project_dir: Path):
        self.home = home
        self.project_dir = project_dir
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
        schema = tool.spec.input_schema
        if schema:
            errors = validate_schema(args or {}, schema)
            if errors:
                result = ToolResult(
                    tool=name,
                    ok=False,
                    error=f"Invalid arguments: {'; '.join(errors)}",
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
            from .safeguards import redact_secrets
            RunStore(self.home).add_tool_call_log(
                tool=result.tool,
                args_json=redact_secrets(json.dumps(args, ensure_ascii=False, sort_keys=True)),
                ok=result.ok,
                facts_json=redact_secrets(json.dumps(result.facts, ensure_ascii=False, sort_keys=True)),
                error=redact_secrets(result.error),
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
        project_dir: Path,
        providers: list[ToolProvider] | None = None,
        allow_sources: set[str] | None = None,
        allowed_tools: set[str] | None = None,
        disabled_tools: set[str] | None = None,
        permission_ceiling: str = "write",
    ):
        self.home = home
        self.project_dir = project_dir
        self.providers = providers or load_tool_providers(home, project_dir=self.project_dir)
        self.allow_sources = allow_sources
        self.allowed_tools = allowed_tools
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
            if self.allowed_tools is not None and tool.spec.name not in self.allowed_tools:
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


def load_tool_providers(home: Path, *, project_dir: Path) -> list[ToolProvider]:
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
    project_dir: Path,
    allow_sources: set[str] | None = None,
    allowed_tools: set[str] | None = None,
    disabled_tools: set[str] | None = None,
    permission_ceiling: str = "write",
) -> ToolGateway:
    return ToolGateway(
        home=home,
        project_dir=project_dir,
        allow_sources=allow_sources,
        allowed_tools=allowed_tools,
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
