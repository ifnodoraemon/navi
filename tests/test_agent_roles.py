from __future__ import annotations

from navi.agent_roles import list_agent_role_names, list_agent_role_specs
from navi.provider import MockProvider, ModelPool
from navi.runtime import AgentRuntime


def test_agent_role_contracts_include_multi_agent_readiness_roles():
    specs = list_agent_role_specs({"planner", "responder", "custom-reviewer"})
    by_name = {spec.name: spec for spec in specs}

    assert {"planner", "critic", "executor", "responder"}.issubset(by_name)
    assert by_name["critic"].parallel_safe is True
    assert by_name["executor"].parallel_safe is False
    assert "findings" in " ".join(by_name["critic"].evidence_required)
    assert by_name["custom-reviewer"].configured_route is True
    assert "custom-reviewer" in list_agent_role_names({"custom-reviewer"})


def test_runtime_exposes_declared_agent_roles_plus_provider_routes(tmp_path):
    runtime = AgentRuntime(
        home=tmp_path,
        provider=ModelPool(default=MockProvider(), routes={"custom-reviewer": MockProvider()}),
    )

    roles = runtime.model_roles()

    assert {"planner", "critic", "executor", "responder", "notification", "custom-reviewer"}.issubset(roles)
