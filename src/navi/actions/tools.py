from __future__ import annotations

from typing import Any, Mapping

from navi.capability_contract import CAPABILITY_ERROR_REASON_KEY
from navi.capabilities_types import Capability, CapabilityContext, CapabilityResult
from navi.runs import RunStore
from navi.tool_manifest import tool_catalog_facts
from navi.tools import ToolSpec


class ToolGatewayCapabilityProvider:
    def __init__(self, gateway, *, capability_registry=None):
        self.gateway = gateway
        self.capability_registry = capability_registry

    def capabilities(self) -> Mapping[str, Capability]:
        return {
            spec.name: ToolCapability(
                spec,
                gateway=self.gateway,
                capability_registry=self.capability_registry,
            )
            for spec in self.gateway.list_specs()
        }


class ToolCapability:
    def __init__(self, spec: ToolSpec, *, gateway, capability_registry=None):
        self.spec = spec
        self.gateway = gateway
        self.capability_registry = capability_registry

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
        elif self.spec.context_policy == "skill_catalog":
            call_args["_skill_permission_ceiling"] = context.skill_permission_ceiling
        elif self.spec.context_policy == "capability_catalog":
            if self.capability_registry is None:
                return CapabilityResult(
                    ok=False,
                    action="tool",
                    facts={CAPABILITY_ERROR_REASON_KEY: "runtime_context_unavailable"},
                    error_reason="runtime_context_unavailable",
                )
            specs = self.capability_registry.planner_specs(
                permission_ceiling=context.permission_ceiling,
            )
            return CapabilityResult(
                ok=True,
                action="tool",
                facts=tool_catalog_facts(
                    specs,
                    definition=(
                        "declared capability catalog; concrete calls remain policy- and "
                        "approval-gated"
                    ),
                    unavailable=self.gateway.list_unavailable(),
                ),
            )
        if self.spec.workspace_scope == "context":
            call_args["_workspace_root"] = context.workspace
        audit_run_id = ""
        if self.capability_registry is not None:
            audit_run_id = self.capability_registry.governed_run_id
        audit_run_id = audit_run_id or context.loop_run_id
        if not audit_run_id and context.approved_approval_id:
            approval = RunStore(context.home).get_approval(context.approved_approval_id)
            audit_run_id = approval.run_id if approval is not None else ""
        result = await self.gateway.call(
            self.spec.name,
            call_args,
            audit_run_id=audit_run_id,
        )
        facts = dict(result.facts or {})
        if result.action == "connector_outbound":
            from navi.connector_delivery import bind_connector_delivery_facts

            facts = bind_connector_delivery_facts(
                facts,
                delivery_id=context.trace_id,
            )
        error_reason = ""
        if not result.ok:
            error_reason = str(
                result.error_reason or facts.get(CAPABILITY_ERROR_REASON_KEY) or "tool_error"
            )
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
