from __future__ import annotations

import json

import pytest

import navi.connector_router as connector_router
from navi.connector_router import ConnectorRouter
from navi.connector_runtime import ConnectorIngressRuntime, ConnectorMessage
from navi.event_bus import EventBus
from navi.provider import ChatMessage
from navi.runtime import AgentRuntime
from navi.runs import RunStore
from navi.trace import TraceStore
from navi.lifecycle import Governance, Phase, Resolution

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
