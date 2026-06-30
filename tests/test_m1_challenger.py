from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.runs import RunStore
from navi.runtime import AgentRuntime
from navi.trace import TraceStore
from navi.workflows import STEP_STATUS_COMPLETED, WORKFLOW_STATUS_VERIFIED_COMPLETE, WorkflowStore
from navi.weixin.config import WeixinConfig
from navi.weixin.service import WeixinService


class NoModelCalls:
    async def complete_for(self, role: str, messages: list[Any], **kwargs: Any) -> str:
        raise AssertionError(f"unexpected model call in service initialization: {role}")

    def list_roles(self) -> list[str]:
        return []


class WorkflowStepProvider:
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
                    "reason": "inspect provider facts for the workflow step",
                }
            )
        return json.dumps(
            {
                "tool": "final.answer",
                "permission": "read",
                "args": {"message": "workflow evidence complete"},
                "model_role": "auditor",
                "confidence": 1.0,
                "reason": "provider facts have been inspected",
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "auditor"]


@pytest.mark.asyncio
async def test_remote_connector_prepare_allowlist_blocks_execution_and_cleanup(
    tmp_path: Path,
) -> None:
    """Remote connectors expose an explicit preparation/read allowlist.

    Execution and cleanup are not remote model syscalls by default; explicit
    approval/control paths handle those state transitions.
    """
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

    run_result = await registry.invoke(
        "delegate.run",
        {"run_id": spawned.run_id},
        permission="write",
        context=context,
    )
    assert run_result.ok is False
    assert run_result.error_reason == "remote_tool_not_allowed"

    pending_delete = await registry.invoke(
        "delegate.delete",
        {"run_id": spawned.run_id, "reason": "remote cleanup attempt"},
        permission="write",
        context=context,
    )
    assert pending_delete.ok is False
    assert pending_delete.error_reason == "remote_tool_not_allowed"
    assert RunStore(tmp_path).get(spawned.run_id) is not None

    runs = RunStore(tmp_path)
    runs.update_run(spawned.run_id, status="failed")
    failed_delete = await registry.invoke(
        "delegate.delete",
        {"run_id": spawned.run_id, "reason": "remove failed delegation record"},
        permission="write",
        context=context,
    )
    assert failed_delete.ok is False
    assert failed_delete.error_reason == "remote_tool_not_allowed"
    assert runs.get(spawned.run_id) is not None


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
async def test_workflow_run_uses_model_owned_step_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = WorkflowStepProvider()
    monkeypatch.setattr("navi.provider.build_provider", lambda config: provider)
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        peer_id="cli",
        sender_id="cli",
        source="cli",
        permission_ceiling="write",
        workspace=str(tmp_path),
        trace_id="workflow-outer-trace",
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
                    "allowed_tools": ["provider.config"],
                    "tool_calls": [
                        {
                            "tool": "provider.config",
                            "permission": "read",
                            "args": {},
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
    workflow_evidence = json.loads(workflow.evidence_json)
    checker_names = [item["name"] for item in workflow_evidence["checker_results"]]
    assert checker_names == [
        "workflow_status_completed",
        "workflow_steps_completed",
        "workflow_step_evidence_present",
        "workflow_capability_evidence_present",
    ]
    assert all(item["passed"] for item in workflow_evidence["checker_results"])
    steps = store.list_steps(workflow_id)
    assert len(steps) == 1
    assert steps[0].status == STEP_STATUS_COMPLETED
    assert provider.planner_calls == 2

    evidence = json.loads(steps[0].evidence_json)
    tool_names = [item["tool"] for item in evidence["evidence"]]
    assert tool_names == ["provider.config", "final.answer"]
    assert evidence["trace_id"]
    assert evidence["summary"] == "workflow evidence complete"
    outer_decisions = [
        json.loads(event.output_json)
        for event in TraceStore(tmp_path).list_loop_decisions("workflow-outer-trace")
    ]
    assert any(
        item["phase"] == "workflow.verify"
        and item["decision"] == "finalize"
        and item["reason"] == "workflow_verifier_passed"
        for item in outer_decisions
    )
