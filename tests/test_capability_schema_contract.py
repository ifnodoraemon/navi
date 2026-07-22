from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext, CapabilityResult
from navi.tools import API_CONTEXT, SideEffectPolicy, ToolSpec


class _SchemaTestCapability:
    def __init__(self, spec: ToolSpec, *, facts: dict[str, Any]):
        self.spec = spec
        self.facts = facts
        self.called = False

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        del args, permission, context
        self.called = True
        return CapabilityResult(
            ok=True,
            action="schema.test",
            facts=self.facts,
        )


def _spec() -> ToolSpec:
    return ToolSpec(
        name="schema.test",
        capability_class="test",
        execution_contexts=("turn",),
        description="schema contract test capability",
        input_schema={
            "type": "object",
            "required": ["count"],
            "properties": {"count": {"type": "integer"}},
        },
        output_schema={
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "integer"}},
        },
        facts_only=True,
        mutates=False,
        permission="read",
        source="action",
    )


def test_mutating_tool_spec_defaults_to_local_side_effect_policy() -> None:
    spec = ToolSpec(
        name="schema.mutate",
        capability_class="test",
        execution_contexts=("turn",),
        description="mutating schema contract test capability",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object", "properties": {}},
        mutates=True,
        permission="write",
    )

    assert isinstance(spec.side_effect_policy, SideEffectPolicy)
    assert spec.side_effect_policy.to_dict() == {
        "scope": "local_state",
        "mode": "immediate",
        "state_field": "",
        "artifact_field": "",
        "commit_tool": "",
        "compensate_tool": "",
        "description": "Capability mutates local durable state immediately.",
    }


def test_connector_delivery_declares_synchronous_external_side_effect(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    spec = registry.get("channel.send_file")

    assert spec is not None
    policy = spec.side_effect_policy
    assert policy.scope == "external"
    assert policy.mode == "synchronous"
    assert policy.state_field == "side_effect_state"
    assert policy.artifact_field == "source_path"
    assert policy.commit_tool == ""
    assert policy.compensate_tool == ""
    assert spec.source == "core.connector_delivery"


def test_respond_is_user_facing_effect_not_facts_only(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    spec = registry.get("respond")

    assert spec is not None
    assert spec.capability_class == "conversation"
    assert spec.facts_only is False
    assert spec.mutates is False


@pytest.mark.parametrize("execution_context", ["turn", API_CONTEXT])
def test_every_public_capability_has_closed_root_schemas(
    tmp_path: Path,
    execution_context: str,
) -> None:
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        execution_context=execution_context,
    )

    for spec in registry.list_specs():
        for label, schema in (
            ("input", spec.input_schema),
            ("output", spec.output_schema),
        ):
            assert schema.get("type") == "object", f"{spec.name} {label} is not an object"
            assert isinstance(schema.get("properties"), dict), (
                f"{spec.name} {label} has no declared properties"
            )
            assert schema.get("additionalProperties") is False, (
                f"{spec.name} {label} accepts undeclared root fields"
            )


def test_only_approval_control_plane_capabilities_bypass_their_own_gate(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    control_plane = {
        spec.name
        for spec in registry.list_specs()
        if spec.approval_policy == "control_plane"
    }

    assert control_plane == {
        "approval.request",
        "approval.resolve",
        "session.request_elevation",
    }
    assert registry.get("goal.resume").confirmation_required is True
    assert registry.get("goal.cancel").confirmation_required is True


@pytest.mark.asyncio
async def test_action_capability_input_schema_is_runtime_contract(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    spec = _spec()
    handler = _SchemaTestCapability(spec, facts={"value": 1})
    registry.handlers = {spec.name: handler}

    result = await registry.invoke(
        spec.name,
        {"count": "1"},
        permission="read",
        context=CapabilityContext(home=tmp_path, workspace=str(tmp_path)),
    )

    assert handler.called is False
    assert result.ok is False
    assert result.error_reason == "schema_mismatch"
    assert result.facts is not None
    assert result.facts["tool"] == spec.name
    assert result.facts["schema_errors"] == ["$.count expected integer"]


@pytest.mark.asyncio
async def test_action_capability_output_schema_is_runtime_contract(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    spec = _spec()
    handler = _SchemaTestCapability(spec, facts={"value": "1"})
    registry.handlers = {spec.name: handler}

    result = await registry.invoke(
        spec.name,
        {"count": 1},
        permission="read",
        context=CapabilityContext(home=tmp_path, workspace=str(tmp_path)),
    )

    assert handler.called is True
    assert result.ok is False
    assert result.error_reason == "schema_mismatch"
    assert result.facts is not None
    assert result.facts["tool"] == spec.name
    assert result.facts["result_action"] == "schema.test"
    assert result.facts["schema_errors"] == ["$.value expected integer"]
