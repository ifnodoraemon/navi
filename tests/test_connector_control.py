from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

import navi.connector_router as connector_router
from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.connector_router import ConnectorRouter
from navi.connector_runtime import ConnectorIngressRuntime, ConnectorMessage
from navi.event_bus import EventBus
from navi.provider import ChatMessage
from navi.runtime import AgentRuntime
from navi.runs import RunStore
from navi.trace import TraceStore
from navi.lifecycle import Governance, Phase, Resolution


class _ConnectorDeleteProvider:
    def __init__(self, target: Path) -> None:
        self.target = target
        self.calls: list[str] = []

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        self.calls.append(role)
        assert role == "planner"
        return json.dumps(
            {
                "syscalls": [
                    {
                        "tool": "shell.run",
                        "permission": "write",
                        "args": {
                            "command": ["rm", str(self.target)],
                            "cwd": str(self.target.parent),
                            "timeout_seconds": 10,
                        },
                        "model_role": "executor",
                        "reason": "execute the exact approved delete",
                    }
                ]
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "executor", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class _BareApprovalProvider(_ConnectorDeleteProvider):
    def __init__(self, target: Path) -> None:
        super().__init__(target)
        self.approval_run_id = ""
        self.planner_calls = 0

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        self.calls.append(role)
        if role == "planner":
            self.planner_calls += 1
            if self.planner_calls == 1:
                return json.dumps(
                    {
                        "syscalls": [
                            {
                                "tool": "shell.run",
                                "permission": "write",
                                "args": {
                                    "command": ["rm", str(self.target)],
                                    "cwd": str(self.target.parent),
                                    "timeout_seconds": 10,
                                },
                                "model_role": "executor",
                                "reason": "request approval for the exact delete",
                            }
                        ]
                    }
                )
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "approval.resolve",
                            "permission": "prepare",
                            "args": {
                                "decision": "approve",
                                "run_id": self.approval_run_id,
                                "selection": "current_run",
                            },
                            "model_role": "executor",
                            "reason": "apply the user's explicit approval",
                        }
                    ]
                }
            )
        if role == "checker":
            return json.dumps(
                {
                    "passed": True,
                    "should_continue": False,
                    "next_step_hint": "",
                    "user_message": "原删除任务已执行并验证。",
                    "evidence_summary": "approval continuation completed",
                }
            )
        if role == "responder":
            return "原删除任务已执行并验证。"
        raise AssertionError(f"unexpected role: {role}")


def _missing_file_verification(target: Path) -> str:
    script = f"from pathlib import Path; assert not Path({str(target)!r}).exists()"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

@pytest.mark.parametrize("approval_text", ["批准 {code}", "批准{code}", "approve {code}", "approve{code}"])
@pytest.mark.asyncio
async def test_connector_approval_command_resolves_matching_pending_approval(tmp_path, approval_text):
    runs = RunStore(tmp_path)
    run = runs.create(
        "needs approval",
        kind="delegation",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase=Phase.PAUSED,
        governance=Governance.AWAITING_APPROVAL,
        resolution=Resolution.BLOCKED,
    )
    approval = runs.create_approval(
        run_id=run.id,
        action="run_execution",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        reason="execute approved task",
    )
    class ApprovalProvider:
        def __init__(self):
            self.calls = 0

        async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
            self.calls += 1
            raise AssertionError("connector approval control envelope should not call the model")
        def list_roles(self) -> list[str]:
            return ["planner"]

    ingress = ConnectorIngressRuntime(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=ApprovalProvider()),
        project_dir=tmp_path,
    )
    try:
        response = await ingress.handle(
            ConnectorMessage(
                message_id="msg-approval",
                peer_id="peer-1",
                sender_id="sender-1",
                text=approval_text.format(code=approval.code),
                source="weixin",
                session_alias_prefix="connector:weixin",
            )
        )
    finally:
        await ingress.event_bus.shutdown()

        assert response.text.startswith("approval_resolved\n")
        assert f"approval_id={approval.id}" in response.text
        assert "decision=approve" in response.text
        assert "status=approved" in response.text
        assert ingress.agent.runtime.provider.calls == 0
        print(list(TraceStore(tmp_path).list_events(trace_id="msg-approval")))
        assert RunStore(tmp_path).get_approval(approval.id).status == "approved"
    updated = RunStore(tmp_path).get(run.id)
    assert updated.phase == Phase.PENDING
    assert updated.governance == Governance.APPROVED
    assert updated.resolution == Resolution.NONE


@pytest.mark.asyncio
async def test_connector_approval_resumes_original_goal_before_reply(tmp_path: Path):
    target = tmp_path / "connector-report.md"
    target.write_text("delete only after approval\n", encoding="utf-8")
    provider = _ConnectorDeleteProvider(target)
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=runtime,
    )
    context = CapabilityContext(
        home=tmp_path,
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        permission_ceiling="write",
        workspace=str(tmp_path),
        trace_id="request-delete",
        enforce_connector_source_policy=False,
    )
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "delete connector report",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["shell.run"],
            "verification_command": _missing_file_verification(target),
        },
        permission="prepare",
        context=context,
    )
    approval = RunStore(tmp_path).pending_approval_for_run(opened.run_id)
    assert approval is not None
    assert target.exists()

    ingress = ConnectorIngressRuntime(
        home=tmp_path,
        runtime=runtime,
        project_dir=tmp_path,
    )
    try:
        response = await ingress.handle(
            ConnectorMessage(
                message_id="approve-delete",
                peer_id="peer-1",
                sender_id="sender-1",
                text=f"批准 {approval.code}",
                source="weixin",
                session_alias_prefix="connector:weixin",
            )
        )
    finally:
        await ingress.event_bus.shutdown()

    assert response is not None
    assert response.text == "loop converged"
    assert target.exists() is False
    original = RunStore(tmp_path).get(opened.run_id)
    assert original is not None
    assert original.phase == Phase.ENDED
    assert original.resolution == Resolution.SUCCESS
    resumed_shell = [
        event
        for event in TraceStore(tmp_path).list_events(trace_id="approve-delete")
        if event.tool == "shell.run" and event.phase == "capability.result"
    ]
    assert len(resumed_shell) == 1
    assert resumed_shell[0].ok is True
    assert provider.calls == ["planner"]


@pytest.mark.asyncio
async def test_connector_bare_approval_model_path_resumes_original_goal(tmp_path: Path):
    target = tmp_path / "bare-approval-report.md"
    target.write_text("delete after bare approval\n", encoding="utf-8")
    provider = _BareApprovalProvider(target)
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=runtime,
    )
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "delete report after approval",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["shell.run"],
            "verification_command": _missing_file_verification(target),
        },
        permission="prepare",
        context=CapabilityContext(
            home=tmp_path,
            source="weixin",
            peer_id="peer-1",
            sender_id="sender-1",
            permission_ceiling="write",
            workspace=str(tmp_path),
            enforce_connector_source_policy=False,
        ),
    )
    provider.approval_run_id = opened.run_id
    assert RunStore(tmp_path).pending_approval_for_run(opened.run_id) is not None

    ingress = ConnectorIngressRuntime(
        home=tmp_path,
        runtime=runtime,
        project_dir=tmp_path,
    )
    try:
        response = await ingress.handle(
            ConnectorMessage(
                message_id="approve-bare",
                peer_id="peer-1",
                sender_id="sender-1",
                text="批准",
                source="weixin",
                session_alias_prefix="connector:weixin",
            )
        )
    finally:
        await ingress.event_bus.shutdown()

    assert response is not None
    assert response.text == "原删除任务已执行并验证。"
    assert target.exists() is False
    original = RunStore(tmp_path).get(opened.run_id)
    assert original is not None
    assert original.resolution == Resolution.SUCCESS
    assert provider.calls == ["planner", "planner", "checker", "responder"]


@pytest.mark.asyncio
async def test_connector_approval_command_returns_not_found_fact(tmp_path):
    class ApprovalProvider:
        def __init__(self):
            self.calls = 0

        async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
            self.calls += 1
            raise AssertionError("connector approval control envelope should not call the model")
        def list_roles(self) -> list[str]:
            return ["planner", "responder"]

    ingress = ConnectorIngressRuntime(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=ApprovalProvider()),
        project_dir=tmp_path,
    )
    try:
        response = await ingress.handle(
            ConnectorMessage(
                message_id="msg-approval",
                peer_id="peer-1",
                sender_id="sender-1",
                text="批准 123456",
                source="weixin",
                session_alias_prefix="connector:weixin",
            )
        )
    finally:
        await ingress.event_bus.shutdown()

    assert response.text.startswith("approval_not_resolved\n")
    assert "reason=approval_code_not_found" in response.text
    assert ingress.agent.runtime.provider.calls == 0


@pytest.mark.asyncio
async def test_connector_timeout_surfaces_structured_fact(tmp_path, monkeypatch):
    monkeypatch.setattr(connector_router, "IDLE_TIMEOUT_SECONDS", 0.01)
    router = ConnectorRouter(tmp_path, EventBus())

    response = await router.route(
        ConnectorMessage(
            message_id="msg-timeout",
            peer_id="peer-1",
            sender_id="sender-1",
            text="hello",
            source="weixin",
            session_alias_prefix="connector:weixin",
        )
    )

    assert response is None


@pytest.mark.asyncio
async def test_connector_runtime_exception_surfaces_structured_fact(tmp_path):
    class NoModelCalls:
        def list_roles(self) -> list[str]:
            return ["planner"]

    ingress = ConnectorIngressRuntime(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
    )

    async def fail_handle(*args, **kwargs):
        raise RuntimeError("boom")

    ingress.agent.handle = fail_handle

    try:
        response = await ingress.handle(
            ConnectorMessage(
                message_id="msg-failure",
                peer_id="peer-1",
                sender_id="sender-1",
                text="hello",
                source="weixin",
                session_alias_prefix="connector:weixin",
            )
        )
    finally:
        await ingress.event_bus.shutdown()

    assert response.text == ""
