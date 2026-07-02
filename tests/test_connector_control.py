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


@pytest.mark.asyncio
async def test_connector_approval_command_resolves_matching_pending_approval(tmp_path):
    runs = RunStore(tmp_path)
    run = runs.create(
        "needs approval",
        kind="delegation",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        status="awaiting_approval",
    )
    approval = runs.create_approval(
        run_id=run.id,
        action="run_execution",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        reason="execute approved task",
    )
    router = ConnectorRouter(tmp_path, EventBus())

    response = await router.route(
        ConnectorMessage(
            message_id="msg-approval",
            peer_id="peer-1",
            sender_id="sender-1",
            text=f"批准 {approval.code}",
            source="weixin",
            session_alias_prefix="connector:weixin",
        )
    )

    assert "approval_resolved" in response
    assert f"run_id={run.id}" in response
    assert RunStore(tmp_path).get_approval(approval.id).status == "approved"
    assert RunStore(tmp_path).get(run.id).status == "pending"


@pytest.mark.asyncio
async def test_connector_approval_command_returns_not_found_fact(tmp_path):
    router = ConnectorRouter(tmp_path, EventBus())

    response = await router.route(
        ConnectorMessage(
            message_id="msg-approval",
            peer_id="peer-1",
            sender_id="sender-1",
            text="批准 123456",
            source="weixin",
            session_alias_prefix="connector:weixin",
        )
    )

    assert "approval_not_resolved" in response
    assert "approval_code_not_found" in response


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

    assert "连接器处理超时" not in response
    assert "event=connector_response_timeout" in response
    assert "correlation_id=msg-timeout" in response


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

    assert "连接器处理失败" not in response
    assert "event=connector_turn_failed" in response
    assert "correlation_id=msg-failure" in response
    assert "error_type=RuntimeError" in response


class ElevationProvider:
    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        **kwargs,
    ) -> str:
        assert role == "planner"
        assert kwargs.get("output_schema") is not None
        prompt = "\n".join(message.content for message in messages)
        assert "session.request_elevation" in prompt
        return json.dumps(
            {
                "tool": "session.request_elevation",
                "permission": "read",
                "args": {
                    "target_permission": "write",
                    "reason": "remote request needs governed local filesystem search",
                },
                "model_role": "responder",
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]


class ElevatedManifestProvider:
    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        **kwargs,
    ) -> str:
        assert role == "planner"
        prompt = "\n".join(message.content for message in messages)
        assert "delegate.run" in prompt
        assert "approval.request" in prompt
        assert "shell.run" not in prompt
        return json.dumps(
            {
                "tool": "respond",
                "permission": "read",
                "args": {"message": "elevated manifest observed"},
                "model_role": "responder",
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]


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

    assert "session_elevation_requested" in response
    assert "target_permission=write" in response
    assert "reason=remote request needs governed local filesystem search" in response
    runs = RunStore(tmp_path).list(limit=10)
    assert len(runs) == 1
    assert runs[0].kind == "elevation"
    assert runs[0].status == "awaiting_approval"
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
        status="awaiting_approval",
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

    assert response == "elevated manifest observed"
