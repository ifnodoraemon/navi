"""Canonical public manifest projection for a capability specification."""

from __future__ import annotations

from typing import Any

from .safeguards import capability_safeguard_facts
from .tools import ToolSpec


def tool_manifest_facts(spec: ToolSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        "permission": spec.permission,
        "facts_only": spec.facts_only,
        "mutates": spec.mutates,
        "source": spec.source,
        "side_effect_policy": spec.side_effect_policy.to_dict(),
        "permission_policy": spec.permission_policy,
        "argument_permission_field": spec.argument_permission_field,
        "argument_permissions": dict(spec.argument_permissions),
        "risk_policy": spec.risk_policy,
        "context_policy": spec.context_policy,
        "runtime_policy": spec.runtime_policy,
        "approval_policy": spec.approval_policy,
        "workspace_policy": spec.workspace_policy,
        "workspace_fields": list(spec.workspace_fields),
        "workspace_scope": spec.workspace_scope,
        "delegation_allowed": spec.delegation_allowed,
        "input_properties": sorted((spec.input_schema.get("properties") or {}).keys()),
        "required": list(spec.input_schema.get("required") or []),
        "safeguards": capability_safeguard_facts(spec),
    }
