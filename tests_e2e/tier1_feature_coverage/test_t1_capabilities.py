"""E2E tests for Feature 2 (Capabilities Decomposition)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from navi.capabilities import CapabilityRegistry, CapabilityContext
from navi.runs import RunStore
from navi.tools import API_CONTEXT
from navi.workflows import (
    WorkflowStore,
    WORKFLOW_STATUS_AWAITING_APPROVAL,
    WORKFLOW_STATUS_APPROVED,
    WORKFLOW_STATUS_VERIFIED_COMPLETE,
)


class _WorkflowStepProvider:
    def __init__(self) -> None:
        self.planner_calls = 0

    async def complete_for(self, role: str, messages: list[Any], **kwargs: Any) -> str:
        if role != "planner":
            raise AssertionError(f"unexpected model role in workflow step: {role}")
        self.planner_calls += 1
        if self.planner_calls == 1:
            return json.dumps(
                {
                    "tool": "provider.config",
                    "permission": "read",
                    "args": {},
                    "model_role": "auditor",
                    "confidence": 1.0,
                    "reason": "inspect provider facts",
                }
            )
        return json.dumps(
            {
                "tool": "final.answer",
                "permission": "read",
                "args": {"message": "workflow lifecycle complete"},
                "model_role": "auditor",
                "confidence": 1.0,
                "reason": "step evidence is complete",
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "auditor"]


@pytest.mark.asyncio
async def test_t1_capabilities_registry_loading(navi_home) -> None:
    """Verify that all decomposed capabilities are correctly discovered, loaded, and listed."""
    registry = CapabilityRegistry(home=navi_home, project_dir=Path.cwd())
    
    # Check both list_specs and planner_specs
    specs = registry.list_specs()
    spec_names = {spec.name for spec in specs}
    
    planner_specs = registry.planner_specs()
    planner_spec_names = {spec.name for spec in planner_specs}
    
    expected_capabilities = [
        "final.answer",
        "ask.user",
        "delegate.spawn",
        "delegate.prepare",
        "approval.request",
        "delegate.run",
        "watch.create",
        "watch.delete",
        "workflow.propose",
        "workflow.approve",
        "workflow.run",
    ]
    
    for name in expected_capabilities:
        assert name in spec_names, f"{name} not found in list_specs()"
        assert name in planner_spec_names, f"{name} not found in planner_specs()"


@pytest.mark.asyncio
async def test_t1_api_only_capabilities_are_isolated_from_turn_planner(navi_home) -> None:
    """Verify API mutation capabilities exist only in the explicit API context."""
    turn_registry = CapabilityRegistry(home=navi_home, project_dir=Path.cwd())
    turn_names = {spec.name for spec in turn_registry.list_specs()}

    api_registry = CapabilityRegistry(
        home=navi_home,
        project_dir=Path.cwd(),
        execution_context=API_CONTEXT,
    )
    api_names = {spec.name for spec in api_registry.list_specs()}

    api_only = {
        "session.create",
        "memory.add",
        "trace.evaluate",
        "evolution.propose",
        "evolution.record_evaluation",
        "evolution.apply",
        "evolution.rollback",
    }
    assert api_only <= api_names
    assert api_only.isdisjoint(turn_names)


@pytest.mark.asyncio
async def test_t1_conversation_actions_dispatch(navi_home) -> None:
    """Invoke final_answer and clarify capabilities directly and verify they return correct result."""
    registry = CapabilityRegistry(home=navi_home, project_dir=Path.cwd())
    context = CapabilityContext(
        home=navi_home,
        peer_id="cli",
        sender_id="cli",
        source="cli",
        workspace=str(Path.cwd()),
    )
    
    # Test final_answer (final.answer)
    res_final = await registry.invoke(
        "final.answer",
        {"message": "Hello Final E2E"},
        permission="read",
        context=context,
    )
    assert res_final.ok is True
    assert res_final.action == "chat"
    assert res_final.message == "Hello Final E2E"
    assert res_final.observation == "Hello Final E2E"
    assert res_final.terminal is True
    
    # Test clarify (ask.user)
    res_clarify = await registry.invoke(
        "ask.user",
        {"message": "Please select", "options": ["optionA", "optionB"]},
        permission="read",
        context=context,
    )
    assert res_clarify.ok is True
    assert res_clarify.action == "ask"
    assert "Please select" in res_clarify.message
    assert "Please select" in res_clarify.observation
    assert res_clarify.terminal is True
    assert res_clarify.facts == {"options": ["optionA", "optionB"]}


@pytest.mark.asyncio
@pytest.mark.live_llm
async def test_t1_delegation_actions_flow(navi_home, monkeypatch) -> None:
    """Test delegate_spawn -> delegate_prepare -> delegate_run sequentially and verify state."""
    monkeypatch.setenv("NAVI_MODEL_PROVIDER", "deepseek")
    monkeypatch.setenv("NAVI_MODEL", "deepseek-v4-pro")

    registry = CapabilityRegistry(home=navi_home, project_dir=Path.cwd())
    context = CapabilityContext(
        home=navi_home,
        peer_id="cli",
        sender_id="cli",
        source="cli",
        workspace=str(Path.cwd()),
    )
    runs = RunStore(navi_home)
    
    # 1. delegate_spawn
    spawned = await registry.invoke(
        "delegate.spawn",
        {
            "objective": "E2E delegation task flow",
            "context": "e2e testing delegate",
            "plan": "1. Run delegation step",
            "success_criteria": "Transition passes",
        },
        permission="prepare",
        context=context,
    )
    assert spawned.ok is True
    run_id = spawned.run_id
    assert run_id
    
    # Verify state in RunStore
    run = runs.get(run_id)
    assert run is not None
    assert run.status == "pending"
    
    # 2. delegate_prepare
    prepared = await registry.invoke(
        "delegate.prepare",
        {"run_id": run_id},
        permission="prepare",
        context=context,
    )
    assert prepared.ok is True
    
    # Verify state in RunStore
    run = runs.get(run_id)
    assert run.status == "prepared"
    
    # Grant execution through an explicit L3 test trust level.
    runs.update_run(run_id, autonomy_level="L3")
    
    # 3. delegate_run
    run_res = await registry.invoke(
        "delegate.run",
        {"run_id": run_id},
        permission="write",
        context=context,
    )
    assert run_res.ok is True
    
    # Verify state in RunStore
    run = runs.get(run_id)
    assert run.status == "queued"


@pytest.mark.asyncio
@pytest.mark.live_llm
async def test_t1_approval_actions_flow(navi_home, monkeypatch) -> None:
    """Test delegation approval flow via approval_request and approval_resolve."""
    monkeypatch.setenv("NAVI_MODEL_PROVIDER", "deepseek")
    monkeypatch.setenv("NAVI_MODEL", "deepseek-v4-pro")

    registry = CapabilityRegistry(home=navi_home, project_dir=Path.cwd())
    context = CapabilityContext(
        home=navi_home,
        peer_id="cli",
        sender_id="cli",
        source="cli",
        workspace=str(Path.cwd()),
    )
    runs = RunStore(navi_home)
    
    # 1. Spawn a delegation run
    spawned = await registry.invoke(
        "delegate.spawn",
        {
            "objective": "E2E approval task flow",
            "context": "e2e testing approval",
            "plan": "1. Run approval step",
            "success_criteria": "Transition passes",
        },
        permission="prepare",
        context=context,
    )
    assert spawned.ok is True
    run_id = spawned.run_id
    assert run_id
    
    # 2. Request approval
    req_res = await registry.invoke(
        "approval.request",
        {"run_id": run_id},
        permission="prepare",
        context=context,
    )
    assert req_res.ok is True
    approval_code = req_res.facts["approval"]["code"]
    assert approval_code
    
    run = runs.get(run_id)
    assert run.status == "awaiting_approval"
    
    # 3. Resolve approval (include the approval code in context.input_text)
    context_with_code = CapabilityContext(
        home=navi_home,
        peer_id="cli",
        sender_id="cli",
        source="cli",
        workspace=str(Path.cwd()),
        input_text=f"Please approve code {approval_code}",
    )
    
    resolve_res = await registry.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval_code},
        permission="write",
        context=context_with_code,
    )
    assert resolve_res.ok is True
    
    # Assert run is queued
    run = runs.get(run_id)
    assert run.status == "queued"


@pytest.mark.asyncio
async def test_t1_watch_actions_flow(navi_home) -> None:
    """Test watch create and watch delete flows and assert state in RunStore."""
    registry = CapabilityRegistry(home=navi_home, project_dir=Path.cwd())
    context = CapabilityContext(
        home=navi_home,
        peer_id="cli",
        sender_id="cli",
        source="cli",
        workspace=str(Path.cwd()),
    )
    runs = RunStore(navi_home)
    
    # 1. Create watch
    create_res = await registry.invoke(
        "watch.create",
        {"cron": "*/5 * * * *", "prompt": "E2E test watch"},
        permission="prepare",
        context=context,
    )
    assert create_res.ok is True
    watch_id = create_res.facts["watch_id"]
    assert watch_id
    
    # Assert watch is in RunStore
    watches = runs.list_watches()
    assert len(watches) == 1
    assert watches[0].id == watch_id
    assert watches[0].prompt == "E2E test watch"
    assert watches[0].cron == "*/5 * * * *"
    
    # 2. Delete watch
    delete_res = await registry.invoke(
        "watch.delete",
        {"watch_id": watch_id, "reason": "E2E cleanup"},
        permission="write",
        context=context,
    )
    assert delete_res.ok is True
    
    # Assert watch is deleted from RunStore
    watches = runs.list_watches()
    assert len(watches) == 0


@pytest.mark.asyncio
async def test_t1_workflow_actions_flow(
    navi_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test full workflow lifecycle flow: propose -> approve -> run -> verify."""
    provider = _WorkflowStepProvider()
    monkeypatch.setattr("navi.provider.build_provider", lambda config: provider)
    registry = CapabilityRegistry(home=navi_home, project_dir=Path.cwd())
    context = CapabilityContext(
        home=navi_home,
        peer_id="cli",
        sender_id="cli",
        source="cli",
        workspace=str(Path.cwd()),
    )
    store = WorkflowStore(navi_home)
    
    # 1. Propose workflow
    proposed = await registry.invoke(
        "workflow.propose",
        {
            "objective": "E2E workflow lifecycle",
            "permission_ceiling": "read",
            "steps": [
                {
                    "id": "inspect-provider",
                    "role": "auditor",
                    "objective": "Inspect provider facts",
                    "allowed_tools": ["provider.config"],
                    "tool_calls": [{"tool": "provider.config", "permission": "read", "args": {}}],
                }
            ],
        },
        permission="prepare",
        context=context,
    )
    assert proposed.ok is True
    workflow_id = proposed.facts["workflow_id"]
    assert workflow_id
    
    # Verify initial proposed state in store
    workflow = store.get(workflow_id)
    assert workflow.status == WORKFLOW_STATUS_AWAITING_APPROVAL
    steps = store.list_steps(workflow_id)
    assert len(steps) == 1
    assert steps[0].status == "pending"
    
    # 2. Approve workflow
    approved = await registry.invoke(
        "workflow.approve",
        {"workflow_id": workflow_id, "decision": "approve"},
        permission="write",
        context=context,
    )
    assert approved.ok is True
    
    # Verify state in store
    workflow = store.get(workflow_id)
    assert workflow.status == WORKFLOW_STATUS_APPROVED
    
    # 3. Run workflow
    run_res = await registry.invoke(
        "workflow.run",
        {"workflow_id": workflow_id},
        permission="write",
        context=context,
    )
    assert run_res.ok is True
    
    # Verify run completed
    workflow = store.get(workflow_id)
    assert workflow.status == WORKFLOW_STATUS_VERIFIED_COMPLETE
    steps = store.list_steps(workflow_id)
    assert steps[0].status == "completed"
    assert provider.planner_calls == 2
