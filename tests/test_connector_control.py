from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

import pytest

import navi.connector_router as connector_router
from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.connector_router import ConnectorRouter
from navi.connector_runtime import ConnectorIngressRuntime, ConnectorMessage
from navi.connector_delivery import connector_delivery_from_facts
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
        self.planner_calls = 0

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        self.calls.append(role)
        if role == "responder":
            return "原删除任务已执行并验证。"
        assert role == "planner"
        self.planner_calls += 1
        if self.planner_calls > 1:
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "respond",
                            "permission": "read",
                            "args": {"message": "原删除任务已执行并验证。"},
                            "reason": "surface the verified original task result",
                        }
                    ]
                }
            )
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
                                "reason": "request approval for the exact delete",
                            }
                        ]
                    }
                )
            if self.planner_calls > 2:
                return json.dumps(
                    {
                        "syscalls": [
                            {
                                "tool": "respond",
                                "permission": "read",
                                "args": {"message": "原删除任务已执行并验证。"},
                                "reason": "surface the checker-accepted continuation",
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
                            },
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
                    "evidence_summary": "approval continuation completed",
                }
            )
        if role == "responder":
            return "原删除任务已执行并验证。"
        raise AssertionError(f"unexpected role: {role}")


class _ConnectorFileProvider:
    def __init__(self, target: Path) -> None:
        self.target = target
        self.calls: list[str] = []

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        del messages, kwargs
        self.calls.append(role)
        if role == "planner":
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "channel.send_file",
                            "permission": "write",
                            "args": {
                                "path": str(self.target),
                                "text": "这是你要的文件。",
                            },
                            "reason": "deliver the requested file",
                        }
                    ]
                }
            )
        if role == "checker":
            return json.dumps(
                {
                    "passed": True,
                    "should_continue": False,
                    "evidence_summary": "delivery contract is ready",
                }
            )
        if role == "responder":
            raise AssertionError("a structured delivery must not be replaced by model prose")
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "executor", "checker", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class _FollowupApprovalProvider:
    """Exercise follow-up approval without external delivery."""

    def __init__(self, target: Path, *, bare_approval: bool) -> None:
        self.target = target
        self.bare_approval = bare_approval
        self.approval_run_id = ""
        self.planner_calls = 0
        self.calls: list[str] = []

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        del kwargs
        self.calls.append(role)
        if role == "planner":
            self.planner_calls += 1
            if self.planner_calls == 1:
                return json.dumps(
                    {
                        "syscalls": [
                            {
                                "tool": "shell.run",
                                "permission": "read",
                                "args": {
                                    "command": [
                                        "find",
                                        str(self.target.parent),
                                        "-maxdepth",
                                        "1",
                                        "-name",
                                        self.target.name,
                                    ],
                                    "cwd": str(self.target.parent),
                                },
                                "reason": "locate the requested file",
                            }
                        ]
                    }
                )
            if self.bare_approval and self.approval_run_id:
                return json.dumps(
                    {
                        "syscalls": [
                            {
                                "tool": "approval.resolve",
                                "permission": "prepare",
                                "args": {
                                    "decision": "approve",
                                    "run_id": self.approval_run_id,
                                },
                                "reason": "apply the user's approval",
                            }
                        ]
                    }
                )
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "channel.send_file",
                            "permission": "write",
                            "args": {
                                "path": str(self.target),
                                "text": "这是你要的文件。",
                            },
                            "reason": "send the located file",
                        }
                    ]
                }
            )
        if role == "checker":
            return json.dumps(
                {
                    "passed": False,
                    "evidence_summary": "the file was located but not sent",
                }
            )
        if role == "responder":
            prompt = messages[-1].content
            code_match = re.search(r'"code":\s*"(\d+)"', prompt)
            assert code_match is not None
            return f"发送文件需要再次批准，审批码 {code_match.group(1)}。"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "checker", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class _DeclaredSensitiveShellApprovalProvider:
    def __init__(self, target: Path) -> None:
        self.target = target

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        del kwargs
        if role == "planner":
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "shell.run",
                            "permission": "write",
                            "args": {"command": ["touch", str(self.target)]},
                            "reason": "request the exact operation",
                        }
                    ]
                }
            )
        if role == "responder":
            match = re.search(r'"code":\s*"(\d+)"', messages[-1].content)
            assert match is not None
            return f"该操作需要审批，审批码 {match.group(1)}。"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class _UnrecoverablePlannerContractProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        del messages, kwargs
        self.calls.append(role)
        if role == "planner":
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "respond",
                            "permission": "read",
                            "args": {"message": "first invalid response"},
                            "reason": "first response",
                        },
                        {
                            "tool": "respond",
                            "permission": "read",
                            "args": {"message": "second invalid response"},
                            "reason": "second response",
                        },
                    ]
                }
            )
        if role == "responder":
            return "运行时给出了 planner 合约失败事实，模型据此说明本轮没有完成。"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "checker", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


def _missing_file_verification(target: Path) -> str:
    script = f"from pathlib import Path; assert not Path({str(target)!r}).exists()"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


@pytest.mark.asyncio
async def test_weixin_turn_surfaces_approval_for_declared_sensitive_shell_effect(
    tmp_path: Path,
) -> None:
    target = tmp_path / "must-wait-for-approval.txt"
    runtime = AgentRuntime(
        home=tmp_path,
        provider=_DeclaredSensitiveShellApprovalProvider(target),
    )
    ingress = ConnectorIngressRuntime(
        home=tmp_path,
        runtime=runtime,
        project_dir=tmp_path,
    )
    try:
        response = await ingress.handle(
            ConnectorMessage(
                message_id="underdeclared-approval",
                peer_id="peer-1",
                sender_id="sender-1",
                text="创建文件",
                source="weixin",
                session_alias_prefix="connector:weixin",
            )
        )
    finally:
        await ingress.event_bus.shutdown()

    assert response is not None
    assert "审批码" in response.text
    approvals = RunStore(tmp_path).list_approvals(limit=10)
    assert len(approvals) == 1
    assert approvals[0].requested_permission == "write"
    assert approvals[0].source == "weixin"
    assert approvals[0].peer_id == "peer-1"
    assert target.exists() is False
    response_events = TraceStore(tmp_path).list_events("underdeclared-approval")
    assert any(event.phase == "channel.response_ready" for event in response_events)


@pytest.mark.asyncio
async def test_weixin_internal_loop_failure_is_synthesized_from_facts(tmp_path: Path) -> None:
    provider = _UnrecoverablePlannerContractProvider()
    ingress = ConnectorIngressRuntime(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
    )
    try:
        response = await ingress.handle(
            ConnectorMessage(
                message_id="planner-contract-failure",
                peer_id="peer-1",
                sender_id="sender-1",
                text="我们什么情况下会委派子agent处理任务",
                source="weixin",
                session_alias_prefix="connector:weixin",
            )
        )
    finally:
        await ingress.event_bus.shutdown()

    assert response is not None
    assert response.text == "运行时给出了 planner 合约失败事实，模型据此说明本轮没有完成。"
    assert "Verified Facts" not in response.text
    assert provider.calls.count("planner") == 10
    assert provider.calls[-1] == "responder"
    final_events = [
        event
        for event in TraceStore(tmp_path).list_events("planner-contract-failure")
        if event.phase == "turn.final"
    ]
    assert len(final_events) == 1
    assert final_events[0].ok is False
    assert final_events[0].message == response.text
    assert "Verified Facts" not in final_events[0].message


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
            self.prompt = ""

        async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
            self.calls += 1
            assert role == "responder"
            self.prompt = messages[-1].content
            return "审批已记录，但原任务没有可续跑的执行状态。"

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
                text=approval_text.format(code=approval.code),
                source="weixin",
                session_alias_prefix="connector:weixin",
            )
        )
    finally:
        await ingress.event_bus.shutdown()

    assert response.text == "审批已记录，但原任务没有可续跑的执行状态。"
    assert f'"approval_id": "{approval.id}"' in ingress.agent.runtime.provider.prompt
    assert '"decision": "approve"' in ingress.agent.runtime.provider.prompt
    assert '"status": "approved"' in ingress.agent.runtime.provider.prompt
    assert ingress.agent.runtime.provider.calls == 1
    assert RunStore(tmp_path).get_approval(approval.id).status == "approved"
    updated = RunStore(tmp_path).get(run.id)
    assert updated.phase == Phase.PENDING
    assert updated.governance == Governance.APPROVED
    assert updated.resolution == Resolution.NONE


@pytest.mark.asyncio
async def test_connector_approval_provider_retry_pause_does_not_call_responder(
    tmp_path, monkeypatch
):
    from navi.control import ApprovalResolution

    async def fake_resolve_and_continue(self, **kwargs):
        return ApprovalResolution(
            ok=True,
            facts={
                "approval_id": "approval-x",
                "status": "approved",
                "loop_terminal_state": "paused",
                "retry_gate": {
                    "decision": "pause",
                    "kind": "provider_transport",
                    "retry_count": 1,
                    "max_retries": 1,
                },
            },
        )

    monkeypatch.setattr(
        connector_router.ApprovalService, "resolve_and_continue", fake_resolve_and_continue
    )

    class NoModelCalls:
        def __init__(self):
            self.calls = []

        async def complete_for(self, role, messages, **kwargs):
            self.calls.append(role)
            raise AssertionError(f"unexpected model call: {role}")

        def list_roles(self):
            return ["planner", "responder"]

    provider = NoModelCalls()
    ingress = ConnectorIngressRuntime(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
    )
    try:
        response = await ingress.handle(
            ConnectorMessage(
                message_id="msg-retry-pause",
                peer_id="peer-1",
                sender_id="sender-1",
                text="approve 123456",
                source="weixin",
                session_alias_prefix="connector:weixin",
            )
        )
    finally:
        await ingress.event_bus.shutdown()

    assert provider.calls == []
    assert response.text == ""
    assert response.facts["finalization"]["durable_retry_pending"] is True


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
    )
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "delete connector report",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["shell.run", "respond"],
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
    assert response.text == "原删除任务已执行并验证。"
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
    assert provider.calls == ["planner", "planner"]


@pytest.mark.asyncio
async def test_connector_read_lookup_skips_approval_and_surfaces_delivery_gate(tmp_path: Path):
    target = tmp_path / "resume.md"
    target.write_text("resume\n", encoding="utf-8")
    provider = _FollowupApprovalProvider(target, bare_approval=False)
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=runtime,
    )
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "locate and send my resume",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["shell.run", "channel.send_file"],
        },
        permission="prepare",
        context=CapabilityContext(
            home=tmp_path,
            source="weixin",
            peer_id="peer-1",
            sender_id="sender-1",
            permission_ceiling="write",
            workspace=str(tmp_path),
        ),
    )
    first = RunStore(tmp_path).pending_approval_for_run(opened.run_id)
    assert first is not None
    assert first.requested_tool == "channel.send_file"

    ingress = ConnectorIngressRuntime(
        home=tmp_path,
        runtime=runtime,
        project_dir=tmp_path,
    )
    try:
        response = await ingress.handle(
            ConnectorMessage(
                message_id="approve-locate-explicit",
                peer_id="peer-1",
                sender_id="sender-1",
                text=f"批准 {first.code}",
                source="weixin",
                session_alias_prefix="connector:weixin",
            )
        )
    finally:
        await ingress.event_bus.shutdown()

    assert response is not None
    delivery = connector_delivery_from_facts(response.facts)
    assert delivery is not None
    assert delivery.path == str(target.resolve())
    assert RunStore(tmp_path).pending_approval_for_run(opened.run_id) is None
    assert "responder" not in provider.calls
    assert TraceStore(tmp_path).evaluate_trace("approve-locate-explicit").outcome == "success"


@pytest.mark.asyncio
async def test_connector_bare_approval_resolves_the_delivery_gate(tmp_path: Path):
    target = tmp_path / "resume.md"
    target.write_text("resume\n", encoding="utf-8")
    provider = _FollowupApprovalProvider(target, bare_approval=True)
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=runtime,
    )
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "locate and send my resume",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["shell.run", "channel.send_file"],
        },
        permission="prepare",
        context=CapabilityContext(
            home=tmp_path,
            source="weixin",
            peer_id="peer-1",
            sender_id="sender-1",
            permission_ceiling="write",
            workspace=str(tmp_path),
        ),
    )
    provider.approval_run_id = opened.run_id
    first = RunStore(tmp_path).pending_approval_for_run(opened.run_id)
    assert first is not None
    assert first.requested_tool == "channel.send_file"

    ingress = ConnectorIngressRuntime(
        home=tmp_path,
        runtime=runtime,
        project_dir=tmp_path,
    )
    try:
        response = await ingress.handle(
            ConnectorMessage(
                message_id="approve-locate-bare",
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
    delivery = connector_delivery_from_facts(response.facts)
    assert delivery is not None
    assert delivery.path == str(target.resolve())
    assert RunStore(tmp_path).pending_approval_for_run(opened.run_id) is None
    assert provider.calls[-1] == "planner"
    assert TraceStore(tmp_path).evaluate_trace("approve-locate-bare").outcome == "success"


@pytest.mark.asyncio
async def test_connector_approval_preserves_durable_file_delivery_contract(
    tmp_path: Path,
) -> None:
    target = tmp_path / "approved-report.xlsx"
    target.write_bytes(b"report")
    provider = _ConnectorFileProvider(target)
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=runtime,
    )
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "send approved report",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["channel.send_file"],
        },
        permission="prepare",
        context=CapabilityContext(
            home=tmp_path,
            source="weixin",
            peer_id="peer-1",
            sender_id="sender-1",
            permission_ceiling="write",
            workspace=str(tmp_path),
            trace_id="request-file",
        ),
    )
    approval = RunStore(tmp_path).pending_approval_for_run(opened.run_id)
    assert approval is not None
    assert not (tmp_path / "weixin" / "outbox").exists()

    ingress = ConnectorIngressRuntime(
        home=tmp_path,
        runtime=runtime,
        project_dir=tmp_path,
    )
    try:
        response = await ingress.handle(
            ConnectorMessage(
                message_id="approve-file",
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
    assert response.action == "connector_outbound"
    delivery = connector_delivery_from_facts(response.facts)
    assert delivery is not None
    assert delivery.path == str(target.resolve())
    assert delivery.text == "这是你要的文件。"
    assert delivery.delivery_id == opened.facts["loop_run_id"]
    assert delivery.run_id == opened.run_id
    assert "responder" not in provider.calls
    response_events = TraceStore(tmp_path).list_events(trace_id="approve-file")
    assert any(event.phase == "channel.response_ready" for event in response_events)
    assert not any(event.phase == "channel.egress" for event in response_events)


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
            "allowed_capabilities": ["shell.run", "respond"],
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
    assert provider.calls == ["planner", "planner", "planner", "checker"]


@pytest.mark.asyncio
async def test_connector_approval_command_returns_not_found_fact(tmp_path):
    class ApprovalProvider:
        def __init__(self):
            self.calls = 0
            self.prompt = ""

        async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
            self.calls += 1
            assert role == "responder"
            self.prompt = messages[-1].content
            return "没有找到对应的待审批请求。"
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

    assert response.text == "没有找到对应的待审批请求。"
    assert '"reason": "approval_code_not_found"' in ingress.agent.runtime.provider.prompt
    assert ingress.agent.runtime.provider.calls == 1


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

    assert response is not None
    assert response.text == ""
    assert response.facts["entity_type"] == "connector_response_wait"
    assert response.facts["state_transition"] == "timeout"
    assert response.facts["reason"] == "upstream_idle_timeout"
    assert response.facts["model_response_present"] is False
    trace_events = TraceStore(tmp_path).list_events("msg-timeout")
    response_events = [event for event in trace_events if event.phase == "channel.response_ready"]
    assert len(response_events) == 1
    assert response_events[0].ok is False


@pytest.mark.asyncio
async def test_connector_approval_without_runtime_fails_directly(tmp_path):
    router = ConnectorRouter(tmp_path, EventBus())

    with pytest.raises(
        RuntimeError,
        match="connector approval response requires an agent runtime",
    ):
        await router.route(
            ConnectorMessage(
                message_id="msg-approval-no-runtime",
                peer_id="peer-1",
                sender_id="sender-1",
                text="批准 123456",
                source="weixin",
                session_alias_prefix="connector:weixin",
            )
        )


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
    assert response.facts["entity_type"] == "runtime_exception"
    assert response.facts["state_transition"] == "failed"
    assert response.facts["reason"] == "agent_turn_exception"
    assert response.facts["exception_type"] == "RuntimeError"
    assert response.facts["finalization"]["model_response_present"] is False
