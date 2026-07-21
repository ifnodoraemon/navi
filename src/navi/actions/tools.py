from __future__ import annotations

from typing import Any, Mapping

from navi.capability_contract import CAPABILITY_ERROR_REASON_KEY
from navi.capabilities_types import Capability, CapabilityContext, CapabilityResult
from navi.safeguards import capability_safeguard_facts
from navi.tools import ToolSpec


def _tool_error_reason(error: str, facts: dict[str, Any]) -> str:
    existing = facts.get(CAPABILITY_ERROR_REASON_KEY)
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    lowered = error.lower()
    if "invalid arguments" in lowered or "must be" in lowered:
        return "invalid_arguments"
    if "required" in lowered or "is required" in lowered:
        return "missing_required_argument"
    if "not found" in lowered:
        return "not_found"
    if "not a file" in lowered:
        return "not_a_file"
    if "not a directory" in lowered:
        return "not_a_directory"
    if "permission" in lowered:
        return "permission_error"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "blocked" in lowered or "denied" in lowered:
        return "blocked"
    return "tool_error"


class ToolGatewayCapabilityProvider:
    def __init__(self, gateway):
        self.gateway = gateway

    def capabilities(self) -> Mapping[str, Capability]:
        return {
            spec.name: ToolCapability(spec, gateway=self.gateway)
            for spec in self.gateway.list_specs()
        }


class ToolCapability:
    def __init__(self, spec: ToolSpec, *, gateway):
        self.spec = spec
        self.gateway = gateway

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        call_args = dict(args)
        if self.spec.context_policy == "actor_memory":
            from navi.memory.scopes import memory_scopes_for_context

            call_args["_allowed_scopes"] = list(
                memory_scopes_for_context(
                    source=context.source,
                    peer_id=context.peer_id,
                    sender_id=context.sender_id,
                    session_id=context.session_id or "",
                    workspace=context.workspace,
                    home=context.home,
                )
            )
            call_args["_context"] = {
                "source": context.source,
                "peer_id": context.peer_id,
                "sender_id": context.sender_id,
                "session_id": context.session_id or "",
                "workspace": context.workspace,
                "trace_id": context.trace_id,
                "input_text": context.input_text,
            }
        result = await self.gateway.call(self.spec.name, call_args)
        facts = dict(result.facts or {})
        if result.action == "connector_outbound":
            from navi.connector_delivery import bind_connector_delivery_facts

            facts = bind_connector_delivery_facts(
                facts,
                delivery_id=context.trace_id,
            )
        error_reason = ""
        if not result.ok:
            error_reason = _tool_error_reason(result.error, facts)
            facts.setdefault(CAPABILITY_ERROR_REASON_KEY, error_reason)
        payload = {
            "capability": self.spec.name,
            "ok": result.ok,
            "facts": facts,
        }
        if error_reason:
            payload[CAPABILITY_ERROR_REASON_KEY] = error_reason
        return CapabilityResult(
            ok=result.ok,
            action=result.action,
            message=result.message if result.message else (result.error if not result.ok else ""),
            terminal=result.terminal,
            yields_control=result.yields_control,
            facts=facts,
            error_reason=error_reason,
        )


class ToolsListCapability:
    def __init__(self, spec: ToolSpec, *, registry):
        self.spec = spec
        self.registry = registry

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        specs = self.registry.planner_specs(
            permission_ceiling=context.permission_ceiling,
        )
        facts = {
            "category": "tools",
            "definition": "callable capabilities available in the current permission context",
            "not_skills": True,
            "tools": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "permission": spec.permission,
                    "facts_only": spec.facts_only,
                    "mutates": spec.mutates,
                    "source": spec.source,
                    "side_effect_policy": spec.side_effect_policy.to_dict(),
                    "permission_policy": spec.permission_policy,
                    "risk_policy": spec.risk_policy,
                    "context_policy": spec.context_policy,
                    "runtime_policy": spec.runtime_policy,
                    "delegation_allowed": spec.delegation_allowed,
                    "input_properties": sorted((spec.input_schema.get("properties") or {}).keys()),
                    "required": list(spec.input_schema.get("required") or []),
                    "safeguards": capability_safeguard_facts(spec),
                }
                for spec in specs
            ],
            "count": len(specs),
        }
        return CapabilityResult(
            ok=True,
            action="tool",
            facts=facts,
        )
