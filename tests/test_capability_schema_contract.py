from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext, CapabilityResult
from navi.tools import ToolSpec


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
            observation=json.dumps(self.facts, ensure_ascii=False, sort_keys=True),
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
    observation = json.loads(result.observation)
    assert observation["tool"] == spec.name
    assert observation["schema_errors"] == ["$.count expected integer"]


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
    observation = json.loads(result.observation)
    assert observation["tool"] == spec.name
    assert observation["result_action"] == "schema.test"
    assert observation["schema_errors"] == ["$.value expected integer"]
