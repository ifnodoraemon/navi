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
from .capability_contract import CAPABILITY_ERROR_REASON_KEY, CAPABILITY_RETRYABLE_KEY
from .json_utils import json_schema_errors, normalize_json_schema
from .permission_contract import normalize_permission
from .runs import RunStore
from .safeguards import redact_secrets, redact_secrets_deep


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


@dataclass(frozen=True)
class ToolAvailability:
    available: bool = True
    reason_code: str = ""
    detail: str = ""
    requirements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        reason_code = str(self.reason_code or "").strip()
        detail = str(self.detail or "").strip()
        requirements = tuple(
            dict.fromkeys(str(item).strip() for item in self.requirements if str(item).strip())
        )
        if not self.available and not reason_code:
            raise ValueError("unavailable tool must declare a reason_code")
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "detail", detail)
        object.__setattr__(self, "requirements", requirements)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "requirements": list(self.requirements),
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
    side_effect_policy: SideEffectPolicy = field(default_factory=SideEffectPolicy)
    permission_policy: str = "static"
    argument_permission_field: str = ""
    argument_permissions: tuple[tuple[str, str], ...] = ()
    risk_policy: str = "declared"
    context_policy: str = "none"
    runtime_policy: str = "none"
    delegation_allowed: bool = True
    risk_class: str = ""
    sensitive_contexts: tuple[str, ...] = ()
    confirmation_required: bool | None = None
    risk_reason_code: str = ""
    # Only capabilities that create or resolve the approval state itself may
    # use ``control_plane``; every other capability is governed by call risk.
    approval_policy: str = "risk"
    # True only when this capability's own completion_evidence fact can
    # deterministically satisfy an objective-evidence checker tier. This is a
    # runtime short-circuit authority, not a judgment about whether every fact
    # returned by the capability is semantically useful or authoritative.
    deterministic_completion_authority: bool = False
    workspace_policy: str = "none"
    workspace_fields: tuple[str, ...] = ()
    workspace_scope: str = "execution"

    def __post_init__(self) -> None:
        if not self.capability_class.strip():
            raise ValueError(f"tool {self.name!r} must declare capability_class")
        contexts = tuple(dict.fromkeys(str(item).strip() for item in self.execution_contexts))
        contexts = tuple(item for item in contexts if item)
        if not contexts:
            raise ValueError(f"tool {self.name!r} must declare execution_contexts")
        object.__setattr__(self, "execution_contexts", contexts)
        object.__setattr__(self, "permission", normalize_permission(self.permission))
        object.__setattr__(
            self,
            "input_schema",
            normalize_json_schema(self.input_schema, output=False),
        )
        object.__setattr__(
            self,
            "output_schema",
            normalize_json_schema(self.output_schema, output=True),
        )
        permission_policy = str(self.permission_policy or "static").strip()
        if permission_policy not in {
            "static",
            "shell_argv",
            "agent_operation",
            "argument_map",
        }:
            raise ValueError(
                f"tool {self.name!r} declares unsupported permission_policy {permission_policy!r}"
            )
        object.__setattr__(self, "permission_policy", permission_policy)
        argument_permission_field = str(self.argument_permission_field or "").strip()
        argument_permissions = tuple(
            (str(key).strip(), normalize_permission(value))
            for key, value in self.argument_permissions
            if str(key).strip()
        )
        if permission_policy == "argument_map" and (
            not argument_permission_field or not argument_permissions
        ):
            raise ValueError(
                f"tool {self.name!r} argument_map permission policy requires a field and mapping"
            )
        if permission_policy != "argument_map" and (
            argument_permission_field or argument_permissions
        ):
            raise ValueError(
                f"tool {self.name!r} declares argument permissions without argument_map policy"
            )
        object.__setattr__(self, "argument_permission_field", argument_permission_field)
        object.__setattr__(self, "argument_permissions", argument_permissions)
        risk_policy = str(self.risk_policy or "declared").strip()
        if risk_policy not in {
            "declared",
            "workspace_file_write",
            "shell_argv",
            "http_request",
            "agent_operation",
            "argument_permission",
        }:
            raise ValueError(f"tool {self.name!r} declares unsupported risk_policy {risk_policy!r}")
        object.__setattr__(self, "risk_policy", risk_policy)
        context_policy = str(self.context_policy or "none").strip()
        if context_policy not in {
            "none",
            "actor_memory",
            "skill_catalog",
            "capability_catalog",
        }:
            raise ValueError(
                f"tool {self.name!r} declares unsupported context_policy {context_policy!r}"
            )
        object.__setattr__(self, "context_policy", context_policy)
        runtime_policy = str(self.runtime_policy or "none").strip()
        if runtime_policy not in {"none", "required", "when_auto_start"}:
            raise ValueError(
                f"tool {self.name!r} declares unsupported runtime_policy {runtime_policy!r}"
            )
        object.__setattr__(self, "runtime_policy", runtime_policy)
        risk_class = str(self.risk_class or "").strip().lower()
        if risk_class and risk_class not in {"low", "medium", "high"}:
            raise ValueError(f"tool {self.name!r} declares unsupported risk_class {risk_class!r}")
        object.__setattr__(self, "risk_class", risk_class)
        sensitive_contexts = tuple(
            dict.fromkeys(
                str(item).strip() for item in self.sensitive_contexts if str(item).strip()
            )
        )
        object.__setattr__(self, "sensitive_contexts", sensitive_contexts)
        object.__setattr__(self, "risk_reason_code", str(self.risk_reason_code or "").strip())
        approval_policy = str(self.approval_policy or "risk").strip()
        if approval_policy not in {"risk", "control_plane", "explicit_control"}:
            raise ValueError(
                f"tool {self.name!r} declares unsupported approval_policy {approval_policy!r}"
            )
        if approval_policy == "control_plane" and (
            self.confirmation_required is True or risk_class == "high"
        ):
            raise ValueError(
                f"tool {self.name!r} cannot both implement and require capability approval"
            )
        object.__setattr__(self, "approval_policy", approval_policy)
        workspace_policy = str(self.workspace_policy or "none").strip()
        if workspace_policy not in {"none", "paths", "sandbox"}:
            raise ValueError(
                f"tool {self.name!r} declares unsupported workspace_policy {workspace_policy!r}"
            )
        workspace_fields = tuple(
            dict.fromkeys(str(item).strip() for item in self.workspace_fields if str(item).strip())
        )
        if workspace_policy == "paths" and not workspace_fields:
            raise ValueError(f"tool {self.name!r} workspace path policy requires fields")
        object.__setattr__(self, "workspace_policy", workspace_policy)
        object.__setattr__(self, "workspace_fields", workspace_fields)
        workspace_scope = str(self.workspace_scope or "execution").strip()
        if workspace_scope not in {"execution", "context"}:
            raise ValueError(
                f"tool {self.name!r} declares unsupported workspace_scope {workspace_scope!r}"
            )
        if workspace_scope == "context" and workspace_policy == "none":
            raise ValueError(
                f"tool {self.name!r} context workspace scope requires a workspace policy"
            )
        object.__setattr__(self, "workspace_scope", workspace_scope)
        if (
            risk_class or sensitive_contexts or self.confirmation_required is not None
        ) and not self.risk_reason_code:
            raise ValueError(
                f"tool {self.name!r} must declare risk_reason_code with custom risk facts"
            )
        policy = self.side_effect_policy
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
    error_reason: str = ""
    retryable: bool | None = None

    def __post_init__(self) -> None:
        if self.ok:
            return
        facts = dict(self.facts or {})
        reason = str(
            self.error_reason or facts.get(CAPABILITY_ERROR_REASON_KEY) or "tool_error"
        ).strip()
        raw_retryable = facts.get(CAPABILITY_RETRYABLE_KEY)
        retryable = self.retryable if self.retryable is not None else raw_retryable
        retryable = bool(retryable) if retryable is not None else False
        facts[CAPABILITY_ERROR_REASON_KEY] = reason
        facts[CAPABILITY_RETRYABLE_KEY] = retryable
        object.__setattr__(self, "facts", facts)
        object.__setattr__(self, "error_reason", reason)
        object.__setattr__(self, "retryable", retryable)

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
            "error_reason": self.error_reason,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler


@dataclass(frozen=True)
class UnavailableTool:
    spec: ToolSpec
    availability: ToolAvailability


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
        self._unavailable_tools: dict[str, UnavailableTool] = {}

    def register(
        self,
        spec: ToolSpec,
        handler: ToolHandler,
        *,
        availability: ToolAvailability | None = None,
    ) -> None:
        if spec.name in self._tools or spec.name in self._unavailable_tools:
            raise ValueError(f"tool already registered: {spec.name}")
        if availability is not None and not availability.available:
            self._unavailable_tools[spec.name] = UnavailableTool(
                spec=spec,
                availability=availability,
            )
            return
        self._tools[spec.name] = RegisteredTool(spec=spec, handler=handler)

    def list_specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in sorted(self._tools.values(), key=lambda item: item.spec.name)]

    def list_sources(self) -> list[str]:
        return sorted({tool.spec.source for tool in self._tools.values()})

    def registered_tools(self) -> list[RegisteredTool]:
        return [tool for tool in sorted(self._tools.values(), key=lambda item: item.spec.name)]

    def unavailable_tools(self) -> list[UnavailableTool]:
        return sorted(self._unavailable_tools.values(), key=lambda item: item.spec.name)

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
                error_reason="not_found",
            )
            self._audit_call(args or {}, result)
            return result
        schema = tool.spec.input_schema
        if schema:
            public_args = {
                key: value for key, value in (args or {}).items() if not str(key).startswith("_")
            }
            errors = json_schema_errors(public_args, schema)
            if errors:
                result = ToolResult(
                    tool=name,
                    ok=False,
                    error=f"Invalid arguments: {'; '.join(errors)}",
                    started_at=started_at,
                    ended_at=time.time(),
                    error_reason="invalid_arguments",
                )
                self._audit_call(args or {}, result)
                return result
        audit_log_id = ""
        # Imported lazily because safeguards type-checks against ToolSpec.
        from .safeguards import call_mutates

        if call_mutates(tool.spec, args):
            try:
                audit_log_id = self._reserve_mutating_audit(
                    tool.spec,
                    args or {},
                    started_at=started_at,
                )
            except Exception as exc:
                logger.error(
                    "mutating tool audit reservation failed for %s: %s",
                    name,
                    exc,
                    exc_info=True,
                )
                return ToolResult(
                    tool=name,
                    ok=False,
                    error="mutating tool was not executed because audit persistence is unavailable",
                    facts={"audit_phase": "reservation", "error_type": type(exc).__name__},
                    error_reason="audit_unavailable",
                    retryable=True,
                    started_at=started_at,
                    ended_at=time.time(),
                )
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
                error_reason="internal_error",
            )
        if not (result.started_at and result.ended_at):
            result = ToolResult(
                tool=result.tool,
                ok=result.ok,
                facts=result.facts,
                error=result.error,
                action=result.action,
                terminal=result.terminal,
                yields_control=result.yields_control,
                message=result.message,
                error_reason=result.error_reason,
                retryable=result.retryable,
                started_at=started_at,
                ended_at=time.time(),
            )
        if audit_log_id:
            try:
                self._complete_mutating_audit(audit_log_id, result)
            except Exception as exc:
                logger.error(
                    "mutating tool audit completion failed for %s: %s",
                    name,
                    exc,
                    exc_info=True,
                )
                return ToolResult(
                    tool=name,
                    ok=False,
                    error="tool effect completed but its audit outcome could not be persisted",
                    facts={
                        "audit_phase": "completion",
                        "audit_reservation_id": audit_log_id,
                        "effect_result_ok": result.ok,
                        "error_type": type(exc).__name__,
                    },
                    error_reason="audit_completion_failed",
                    retryable=False,
                    started_at=started_at,
                    ended_at=time.time(),
                )
        else:
            self._audit_call(args or {}, result)
        return result

    def _reserve_mutating_audit(
        self,
        spec: ToolSpec,
        args: dict[str, Any],
        *,
        started_at: float,
    ) -> str:
        safe_args = redact_secrets_deep(args)
        log = RunStore(self.home).add_tool_call_log(
            tool=spec.name,
            args_json=json.dumps(safe_args, ensure_ascii=False, sort_keys=True),
            ok=False,
            facts_json=json.dumps(
                {"audit_phase": "reserved", "mutates": True},
                ensure_ascii=False,
                sort_keys=True,
            ),
            error="execution outcome pending",
            started_at=started_at,
            ended_at=started_at,
        )
        return log.id

    def _complete_mutating_audit(self, log_id: str, result: ToolResult) -> None:
        RunStore(self.home).complete_tool_call_log(
            log_id,
            ok=result.ok,
            facts_json=json.dumps(
                {"audit_phase": "completed", **redact_secrets_deep(result.facts)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            error=redact_secrets(result.error),
            ended_at=result.ended_at,
        )

    def _audit_call(self, args: dict[str, Any], result: ToolResult) -> None:
        try:
            # Redact at the value level before serialization so secrets
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
        for unavailable_tool in raw.unavailable_tools():
            self.registry.register(
                unavailable_tool.spec,
                lambda args: ToolResult(tool="unavailable", ok=False),
                availability=unavailable_tool.availability,
            )

    def list_specs(self) -> list[ToolSpec]:
        return self.registry.list_specs()

    def list_sources(self) -> list[str]:
        return self.registry.list_sources()

    def list_unavailable(self) -> list[UnavailableTool]:
        return self.registry.unavailable_tools()

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
