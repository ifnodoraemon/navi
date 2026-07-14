from __future__ import annotations

import inspect
import time
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .connector_registry import load_connector_adapters
from .runs import RunStore


logger = logging.getLogger(__name__)
ToolHandler = Callable[[dict[str, Any]], "Awaitable[ToolResult] | ToolResult"]
TURN_CONTEXT = "turn"
ACTUATOR_CONTEXT = "actuator"
CONTROL_PLANE_CONTEXT = "control_plane"
API_CONTEXT = "api"
ALL_EXECUTION_CONTEXTS = (
    TURN_CONTEXT,
    ACTUATOR_CONTEXT,
    CONTROL_PLANE_CONTEXT,
)


@dataclass(frozen=True)
class SideEffectPolicy:
    scope: str = "none"
    mode: str = "none"
    state_field: str = ""
    artifact_field: str = ""
    commit_tool: str = ""
    compensate_tool: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", str(self.scope or "none").strip() or "none")
        object.__setattr__(self, "mode", str(self.mode or "none").strip() or "none")
        object.__setattr__(self, "state_field", str(self.state_field or "").strip())
        object.__setattr__(self, "artifact_field", str(self.artifact_field or "").strip())
        object.__setattr__(self, "commit_tool", str(self.commit_tool or "").strip())
        object.__setattr__(self, "compensate_tool", str(self.compensate_tool or "").strip())
        object.__setattr__(self, "description", str(self.description or "").strip())

    def to_dict(self) -> dict[str, str]:
        return {
            "scope": self.scope,
            "mode": self.mode,
            "state_field": self.state_field,
            "artifact_field": self.artifact_field,
            "commit_tool": self.commit_tool,
            "compensate_tool": self.compensate_tool,
            "description": self.description,
        }


def validate_schema(data: Any, schema: dict[str, Any], path: str = "") -> list[str]:
    """Validates data against a simplified JSON Schema. Returns list of error messages."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors
    expected_type = schema.get("type")
    if not isinstance(expected_type, str):
        return errors
    validator = _TYPE_VALIDATORS.get(expected_type)
    if validator is None:
        return errors
    return validator(data, schema, path, errors)


def _validate_object(
    data: Any, schema: dict[str, Any], path: str, errors: list[str]
) -> list[str]:
    if not isinstance(data, dict):
        errors.append(f"'{path or 'input'}' must be an object, got {type(data).__name__}")
        return errors
    required = schema.get("required", [])
    for req_field in required:
        if req_field not in data:
            errors.append(f"'{path or 'input'}' is missing required property: {req_field}")
    properties = schema.get("properties", {})
    for key, val in data.items():
        if key in properties:
            errors.extend(
                validate_schema(val, properties[key], f"{path}.{key}" if path else key)
            )
    return errors


def _validate_array(
    data: Any, schema: dict[str, Any], path: str, errors: list[str]
) -> list[str]:
    if not isinstance(data, (list, tuple)):
        errors.append(f"'{path or 'input'}' must be an array, got {type(data).__name__}")
        return errors
    items = schema.get("items")
    if items:
        for idx, item in enumerate(data):
            errors.extend(validate_schema(item, items, f"{path}[{idx}]"))
    return errors


def _validate_string(
    data: Any, schema: dict[str, Any], path: str, errors: list[str]
) -> list[str]:
    if not isinstance(data, str):
        errors.append(f"'{path or 'input'}' must be a string, got {type(data).__name__}")
    return errors


def _validate_integer(
    data: Any, schema: dict[str, Any], path: str, errors: list[str]
) -> list[str]:
    if not isinstance(data, int) or isinstance(data, bool):
        errors.append(f"'{path or 'input'}' must be an integer, got {type(data).__name__}")
    return errors


def _validate_number(
    data: Any, schema: dict[str, Any], path: str, errors: list[str]
) -> list[str]:
    if not isinstance(data, (int, float)) or isinstance(data, bool):
        errors.append(f"'{path or 'input'}' must be a number, got {type(data).__name__}")
    return errors


def _validate_boolean(
    data: Any, schema: dict[str, Any], path: str, errors: list[str]
) -> list[str]:
    if not isinstance(data, bool):
        errors.append(f"'{path or 'input'}' must be a boolean, got {type(data).__name__}")
    return errors


_TYPE_VALIDATORS: dict[str, Callable[..., list[str]]] = {
    "object": _validate_object,
    "array": _validate_array,
    "string": _validate_string,
    "integer": _validate_integer,
    "number": _validate_number,
    "boolean": _validate_boolean,
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    capability_class: str
    execution_contexts: tuple[str, ...]
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    facts_only: bool = True
    mutates: bool = False
    permission: str = "read"
    source: str = "core"
    side_effect_policy: SideEffectPolicy | dict[str, Any] = field(
        default_factory=SideEffectPolicy
    )
    # Governance primitives carry their own first-level guard and must not be
    # suspended by the approval mechanism they implement — that creates an
    # infinite approval loop. Declared per-spec so the exemption is data-driven,
    # not a hardcoded name set (principle 1.1/6).
    governance_exempt: bool = False

    def __post_init__(self) -> None:
        if not self.capability_class.strip():
            raise ValueError(f"tool {self.name!r} must declare capability_class")
        contexts = tuple(dict.fromkeys(str(item).strip() for item in self.execution_contexts))
        contexts = tuple(item for item in contexts if item)
        if not contexts:
            raise ValueError(f"tool {self.name!r} must declare execution_contexts")
        object.__setattr__(self, "execution_contexts", contexts)
        policy = self.side_effect_policy
        if isinstance(policy, dict):
            policy = SideEffectPolicy(**policy)
        if not isinstance(policy, SideEffectPolicy):
            raise ValueError(f"tool {self.name!r} must declare a valid side_effect_policy")
        if self.mutates and policy.scope == "none":
            policy = SideEffectPolicy(
                scope="local_state",
                mode="immediate",
                description="Capability mutates local durable state immediately.",
            )
        object.__setattr__(self, "side_effect_policy", policy)

    def available_in(self, context: str) -> bool:
        return context in self.execution_contexts


@dataclass(frozen=True)
class ToolResult:
    tool: str
    ok: bool
    facts: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    action: str = "tool"
    terminal: bool = False
    yields_control: bool = False
    message: str = ""

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
            "yields_control": self.yields_control,
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

    async def call(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
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
            handler_result = tool.handler(args or {})
            if inspect.isawaitable(handler_result):
                handler_result = await handler_result
            result = handler_result
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
            action=result.action,
            terminal=result.terminal,
            yields_control=result.yields_control,
            message=result.message,
            started_at=started_at,
            ended_at=time.time(),
        )
        self._audit_call(args or {}, result)
        return result

    def _audit_call(self, args: dict[str, Any], result: ToolResult) -> None:
        try:
            from .safeguards import redact_secrets, redact_secrets_deep

            # FP-4/L8: redact at the value level before serialization so secrets
            # nested inside args/facts (not just keyword-prefixed ones) are
            # caught, regardless of key naming or JSON sort order.
            safe_args = redact_secrets_deep(args)
            safe_facts = redact_secrets_deep(result.facts)
            RunStore(self.home).add_tool_call_log(
                tool=result.tool,
                args_json=json.dumps(safe_args, ensure_ascii=False, sort_keys=True),
                ok=result.ok,
                facts_json=json.dumps(safe_facts, ensure_ascii=False, sort_keys=True),
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
    ):
        self.home = home
        self.project_dir = project_dir
        self.providers = providers or load_tool_providers(home, project_dir=self.project_dir)
        self.registry = ToolRegistry(home=home, project_dir=self.project_dir)
        self.refresh()

    def refresh(self) -> None:
        raw = ToolRegistry(home=self.home, project_dir=self.project_dir)
        for provider in self.providers:
            provider.register(raw)
        self.registry = ToolRegistry(home=self.home, project_dir=self.project_dir)
        for tool in raw.registered_tools():
            self.registry.register(tool.spec, tool.handler)

    def list_specs(self) -> list[ToolSpec]:
        return self.registry.list_specs()

    def list_sources(self) -> list[str]:
        return self.registry.list_sources()

    def get(self, name: str) -> ToolSpec | None:
        return self.registry.get(name)

    async def call(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        return await self.registry.call(name, args)


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
        ToolProvider(
            name="mcp",
            source="mcp",
            register=lambda registry: _register_mcp_tools(registry, home=home),
        ),
    ]


def build_tool_gateway(
    home: Path,
    *,
    project_dir: Path,
) -> ToolGateway:
    return ToolGateway(
        home=home,
        project_dir=project_dir,
    )


def _register_core_fact_tools(registry: ToolRegistry, *, home: Path) -> None:
    from .core_tools import register_core_tools

    register_core_tools(registry, home=home)


def _register_connector_tools(registry: ToolRegistry, *, home: Path) -> None:
    for adapter in load_connector_adapters():
        adapter.register_tools(registry, home)


def _register_mcp_tools(registry: ToolRegistry, *, home: Path) -> None:
    from .mcp_tools import register_mcp_tools

    register_mcp_tools(registry, home=home)
