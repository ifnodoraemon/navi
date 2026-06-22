from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.runs import RunStore
from navi.runtime import AgentRuntime
from navi.workflows import STEP_STATUS_COMPLETED, WORKFLOW_STATUS_VERIFIED_COMPLETE, WorkflowStore
from navi.weixin.config import WeixinConfig
from navi.weixin.service import WeixinService


class NoModelCalls:
    async def complete_for(self, role: str, messages: list[Any], **kwargs: Any) -> str:
        raise AssertionError(f"unexpected model call in service initialization: {role}")

    def list_roles(self) -> list[str]:
        return []


@pytest.mark.asyncio
async def test_remote_connector_full_permissions_but_execution_gated(
    tmp_path: Path,
) -> None:
    """Remote connectors have full permissions to governance/read tools
    (auto-loaded from declared specs, no hand-maintained allowlist). Only
    direct-OS classes are blocked from the live remote path. delegate.run is
    invokable but fails without an execution grant — the approval gate still
    holds even with full connector permissions."""
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        peer_id="weixin-peer",
        sender_id="weixin-user",
        source="connector.weixin",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )
    spawned = await registry.invoke(
        "delegate.spawn",
        {
            "objective": "Prepare a tracked task",
            "context": "Remote connector requested tracked work.",
            "plan": "Prepare first; execution needs approval.",
            "success_criteria": "Task is tracked and governed.",
        },
        permission="prepare",
        context=context,
    )
    assert spawned.ok is True
    assert spawned.run_id

    # delegate.run is invokable from remote (full permissions), but without
    # an execution grant (approval) it fails — the approval gate holds.
    run_result = await registry.invoke(
        "delegate.run",
        {"run_id": spawned.run_id},
        permission="write",
        context=context,
    )
    assert run_result.ok is False

    # A run stuck in the transient ``pending`` state must be deletable from a
    # remote surface, otherwise it is an undeletable, unapprovable, uncompletable
    # dead end (the 13 ``delegate.delete`` failures observed in production).
    pending_delete = await registry.invoke(
        "delegate.delete",
        {"run_id": spawned.run_id, "reason": "remote cleanup attempt"},
        permission="write",
        context=context,
    )
    assert pending_delete.ok is True
    assert RunStore(tmp_path).get(spawned.run_id) is None

    # A failed run remains deletable from remote.
    failed_spawn = await registry.invoke(
        "delegate.spawn",
        {
            "objective": "Prepare another tracked task",
            "context": "Remote connector requested tracked work.",
            "plan": "Prepare first; execution needs approval.",
            "success_criteria": "Task is tracked and governed.",
        },
        permission="prepare",
        context=context,
    )
    runs = RunStore(tmp_path)
    runs.update_run(failed_spawn.run_id, status="failed")
    failed_delete = await registry.invoke(
        "delegate.delete",
        {"run_id": failed_spawn.run_id, "reason": "remove failed delegation record"},
        permission="write",
        context=context,
    )

    assert failed_delete.ok is True
    assert runs.get(failed_spawn.run_id) is None


@pytest.mark.asyncio
async def test_bulk_delete_requires_explicit_scope(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        source="cli",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )
    result = await registry.invoke(
        "delegate.delete",
        {"status": "failed", "reason": "cleanup failed delegation records"},
        permission="write",
        context=context,
    )

    assert result.ok is False
    assert "requires source or kind scope" in result.message


def test_weixin_service_initializes_connector_ingress_without_direct_router_call(
    tmp_path: Path,
) -> None:
    runtime = AgentRuntime(home=tmp_path, provider=NoModelCalls())
    injected_client = object()

    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=runtime,
        project_dir=tmp_path,
        client=injected_client,
    )

    assert service.client is injected_client
    assert service.active is service.daemon
    assert service.ingress.agent is not None


@pytest.mark.asyncio
async def test_workflow_run_executes_declared_capability_evidence(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        peer_id="cli",
        sender_id="cli",
        source="cli",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )
    proposed = await registry.invoke(
        "workflow.propose",
        {
            "objective": "Report a verified workflow result",
            "permission_ceiling": "read",
            "steps": [
                {
                    "id": "report",
                    "role": "auditor",
                    "objective": "Return the verified step result.",
                    "allowed_tools": ["final.answer"],
                    "tool_calls": [
                        {
                            "tool": "final.answer",
                            "permission": "read",
                            "args": {"message": "workflow evidence complete"},
                        }
                    ],
                }
            ],
        },
        permission="prepare",
        context=context,
    )
    assert proposed.ok is True
    workflow_id = proposed.facts["workflow_id"]

    approved = await registry.invoke(
        "workflow.approve",
        {"workflow_id": workflow_id, "decision": "approve"},
        permission="write",
        context=context,
    )
    assert approved.ok is True

    run = await registry.invoke(
        "workflow.run",
        {"workflow_id": workflow_id},
        permission="write",
        context=context,
    )
    assert run.ok is True

    store = WorkflowStore(tmp_path)
    workflow = store.get(workflow_id)
    assert workflow is not None
    assert workflow.status == WORKFLOW_STATUS_VERIFIED_COMPLETE
    steps = store.list_steps(workflow_id)
    assert len(steps) == 1
    assert steps[0].status == STEP_STATUS_COMPLETED

    evidence = json.loads(steps[0].evidence_json)
    tool_evidence = evidence["evidence"][0]
    assert tool_evidence["tool"] == "final.answer"
    assert tool_evidence["permission"] == "read"
    assert tool_evidence["ok"] is True
