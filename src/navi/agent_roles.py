from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from .spec_loader import load_spec


@dataclass(frozen=True)
class AgentRoleSpec:
    name: str
    purpose: str
    when_to_use: tuple[str, ...]
    evidence_required: tuple[str, ...]
    parallel_safe: bool
    configured_route: bool = False

    def to_prompt_dict(self) -> dict:
        return asdict(self)


def list_agent_role_specs(configured_routes: Iterable[str] = ()) -> list[AgentRoleSpec]:
    raw = load_spec("agent_roles.yaml") or {}
    roles = raw.get("roles") or {}
    configured = {str(role) for role in configured_routes if str(role)}
    specs: list[AgentRoleSpec] = []
    for name, item in roles.items():
        if not isinstance(item, dict):
            continue
        role_name = str(name)
        specs.append(
            AgentRoleSpec(
                name=role_name,
                purpose=str(item.get("purpose") or ""),
                when_to_use=tuple(str(value) for value in item.get("when_to_use") or ()),
                evidence_required=tuple(str(value) for value in item.get("evidence_required") or ()),
                parallel_safe=bool(item.get("parallel_safe", False)),
                configured_route=role_name in configured,
            )
        )
    known = {spec.name for spec in specs}
    for route in sorted(configured - known):
        specs.append(
            AgentRoleSpec(
                name=route,
                purpose="Configured model route without a dedicated role contract.",
                when_to_use=("Only when explicitly selected by planner policy.",),
                evidence_required=("Trace events must record model_role for this route.",),
                parallel_safe=False,
                configured_route=True,
            )
        )
    return sorted(specs, key=lambda spec: spec.name)


def list_agent_role_names(configured_routes: Iterable[str] = ()) -> list[str]:
    return [spec.name for spec in list_agent_role_specs(configured_routes)]
