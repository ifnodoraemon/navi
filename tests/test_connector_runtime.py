from __future__ import annotations

import pytest

from navi.connector_runtime import ConnectorIngressRuntime, ConnectorMessage
from navi.provider import MockProvider, ModelPool
from navi.runtime import AgentRuntime


@pytest.mark.asyncio
async def test_connector_ingress_runtime_routes_message_to_agent_session(tmp_path):
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=MockProvider()))
    ingress = ConnectorIngressRuntime(home=tmp_path, runtime=runtime, allow_sources={"action", "core"})

    text = await ingress.handle(
        ConnectorMessage(
            message_id="msg-1",
            peer_id="peer",
            sender_id="sender",
            text="hello",
            source="connector.test",
            session_alias_prefix="connector:test",
        )
    )

    session_id = runtime.memory.current_session_id("connector:test:peer")
    assert text == "Navi received: hello"
    assert runtime.memory.get_messages(session_id)[0].content == "hello"


def test_connector_ingress_runtime_uses_remote_tool_allowlist(tmp_path):
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=MockProvider()))
    ingress = ConnectorIngressRuntime(home=tmp_path, runtime=runtime)

    names = {spec.name for spec in ingress.agent.capabilities.planner_specs()}

    assert {
        "final.answer",
        "clarify.ask",
        "task.record",
        "task.prepare",
        "approval.request",
        "task.queue",
        "watch.create",
        "approval.resolve",
    } <= names
    assert {"provider.config", "service.status", "task.status", "task.list"} <= names
    assert "task.delete" not in names
    assert "watch.delete" not in names
    assert "file.read" not in names
    assert "file.write" not in names
    assert "filesystem.list" not in names
    assert "git.status" not in names
    assert "shell.run" not in names
    assert "test.run" not in names
