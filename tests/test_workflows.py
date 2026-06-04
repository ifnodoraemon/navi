from __future__ import annotations

import pytest

from navi.capabilities import CapabilityContext, build_capability_registry
from navi.workflows import (
    WORKFLOW_STATUS_AWAITING_APPROVAL,
    WORKFLOW_STATUS_VERIFIED_COMPLETE,
    WorkflowStore,
)


@pytest.mark.asyncio
async def test_dynamic_workflow_lifecycle_runs_declared_read_capabilities(tmp_path):
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(home=tmp_path, peer_id="cli", sender_id="cli", source="cli", workspace=str(tmp_path))

    proposed = await registry.invoke(
        "workflow.propose",
        {
            "objective": "Audit current provider configuration",
            "permission_ceiling": "read",
            "max_concurrency": 2,
            "steps": [
                {
                    "id": "inspect-provider",
                    "role": "auditor",
                    "objective": "Inspect provider facts",
                    "allowed_tools": ["provider.config"],
                    "tool_calls": [{"tool": "provider.config", "permission": "read", "args": {}}],
                },
                {
                    "id": "inspect-tools",
                    "role": "critic",
                    "objective": "Inspect available tools after provider facts",
                    "depends_on": ["inspect-provider"],
                    "allowed_tools": ["tools.list"],
                    "tool_calls": [{"tool": "tools.list", "permission": "read", "args": {}}],
                },
            ],
        },
        permission="prepare",
        context=context,
    )

    assert proposed.ok
    workflow_id = proposed.facts["workflow_id"]
    assert proposed.facts["status"] == WORKFLOW_STATUS_AWAITING_APPROVAL
    assert proposed.facts["confirmation_required"] is True

    blocked_run = await registry.invoke("workflow.run", {"workflow_id": workflow_id}, permission="write", context=context)
    assert not blocked_run.ok
    assert "approve" in blocked_run.message

    approved = await registry.invoke(
        "workflow.approve",
        {"workflow_id": workflow_id, "decision": "approve"},
        permission="write",
        context=context,
    )
    assert approved.ok

    first = await registry.invoke("workflow.run", {"workflow_id": workflow_id}, permission="write", context=context)
    assert first.ok
    assert first.facts["completed_count"] == 1
    assert first.facts["pending_count"] == 1

    resumed = await registry.invoke("workflow.resume", {"workflow_id": workflow_id}, permission="write", context=context)
    assert resumed.ok
    assert resumed.facts["completed_count"] == 2
    assert resumed.facts["pending_count"] == 0

    verified = await registry.invoke("workflow.verify", {"workflow_id": workflow_id}, permission="write", context=context)
    assert verified.ok
    assert verified.facts["status"] == WORKFLOW_STATUS_VERIFIED_COMPLETE
    assert verified.facts["verifier_passed"] is True

    status = await registry.invoke("workflow.status", {"workflow_id": workflow_id}, permission="read", context=context)
    assert status.ok
    assert status.facts["workflow"]["status"] == WORKFLOW_STATUS_VERIFIED_COMPLETE
    assert status.facts["step_count"] == 2

    events = [event.event_type for event in WorkflowStore(tmp_path).list_events(workflow_id)]
    assert "workflow.proposed" in events
    assert "workflow.verified" in events


@pytest.mark.asyncio
async def test_dynamic_workflow_blocks_undeclared_tool_calls(tmp_path):
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(home=tmp_path, peer_id="cli", sender_id="cli", source="cli", workspace=str(tmp_path))

    proposed = await registry.invoke(
        "workflow.propose",
        {
            "objective": "Try undeclared tool",
            "steps": [
                {
                    "id": "bad-step",
                    "objective": "Call a tool outside the declared allowlist",
                    "allowed_tools": ["provider.config"],
                    "tool_calls": [{"tool": "tools.list", "permission": "read", "args": {}}],
                }
            ],
        },
        permission="prepare",
        context=context,
    )
    workflow_id = proposed.facts["workflow_id"]
    await registry.invoke(
        "workflow.approve",
        {"workflow_id": workflow_id, "decision": "approve"},
        permission="write",
        context=context,
    )

    result = await registry.invoke("workflow.run", {"workflow_id": workflow_id}, permission="write", context=context)

    assert not result.ok or result.facts["status"] == "blocked"
    workflow = WorkflowStore(tmp_path).get(workflow_id)
    assert workflow is not None
    assert workflow.status == "blocked"
