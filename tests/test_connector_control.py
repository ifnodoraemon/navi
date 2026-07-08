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


class ElevationProvider:
    enable_request_router = True

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        **kwargs,
    ) -> str:
        prompt = "\n".join(message.content for message in messages)
        if role == "router":
            return json.dumps(
                {
                    "intent": "request_elevation",
                    "reason": "remote request needs governed local filesystem search",
                    "confidence": 0.99,
                    "facts": {
                        "target_permission": "write",
                        "reason": "remote request needs governed local filesystem search",
                    },
                }
            )
        if role == "planner":
            assert kwargs.get("output_schema") is not None
            assert "session.request_elevation" in prompt
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "session.request_elevation",
                            "permission": "read",
                            "args": {
                                "target_permission": "write",
                                "reason": "remote request needs governed local filesystem search",
                            },
                            "model_role": "responder",
                        }
                    ]
                }
            )
        if role == "responder":
            assert "elevation_requested" in prompt
            assert "target_permission" in prompt
            return "需要审批后才能继续。"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class ElevatedManifestProvider:
    enable_request_router = True

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        **kwargs,
    ) -> str:
        if role == "router":
            return json.dumps(
                {
                    "intent": "answer_now",
                    "reason": "approved elevation is present in current state",
                    "confidence": 0.99,
                    "facts": {},
                }
            )
        assert role == "responder"
        prompt = "\n".join(message.content for message in messages)
        assert "shell.run" not in prompt
        return "elevated manifest observed"

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


@pytest.mark.asyncio
async def test_connector_request_needing_local_access_surfaces_elevation_fact(tmp_path):
    ingress = ConnectorIngressRuntime(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=ElevationProvider()),
        project_dir=tmp_path,
    )

    try:
        response = await ingress.handle(
            ConnectorMessage(
                message_id="msg-direct-search",
                peer_id="peer-1",
                sender_id="sender-1",
                text="直接找",
                source="weixin",
                session_alias_prefix="connector:weixin",
                facts={"connector": "weixin"},
            )
        )
    finally:
        await ingress.event_bus.shutdown()

    assert response.text == "需要审批后才能继续。"
    assert "session_elevation_requested" not in response.text
    assert "target_permission=write" not in response.text
    runs = RunStore(tmp_path).list(limit=10)
    assert len(runs) == 1
    assert runs[0].kind == "elevation"
    assert runs[0].phase == Phase.PAUSED
    assert runs[0].governance == Governance.AWAITING_APPROVAL
    assert runs[0].resolution == Resolution.BLOCKED
    approvals = RunStore(tmp_path).list_approvals(run_id=runs[0].id)
    assert len(approvals) == 1
    assert approvals[0].action == "session_elevation"


@pytest.mark.asyncio
async def test_connector_approved_session_elevation_reaches_planner_manifest(tmp_path):
    runs = RunStore(tmp_path)
    run = runs.create(
        "elevate remote session",
        kind="elevation",
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
        action="session_elevation",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        requested_permission="write",
        reason="allow governed local continuation",
    )
    runs.resolve_approval(approval.id, decision="approve", resolved_by="sender-1")
    ingress = ConnectorIngressRuntime(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=ElevatedManifestProvider()),
        project_dir=tmp_path,
    )

    try:
        response = await ingress.handle(
            ConnectorMessage(
                message_id="msg-after-approval",
                peer_id="peer-1",
                sender_id="sender-1",
                text="继续",
                source="weixin",
                session_alias_prefix="connector:weixin",
            )
        )
    finally:
        await ingress.event_bus.shutdown()

    assert response.text == "elevated manifest observed"
