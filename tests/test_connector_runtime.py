from __future__ import annotations

import pytest

from navi.connector_runtime import ConnectorIngressRuntime, ConnectorMessage, REMOTE_CONNECTOR_TOOL_POLICY
from navi.capabilities import CapabilityContext
from navi.provider import MockProvider, ModelPool
from navi.runtime import AgentRuntime


@pytest.mark.asyncio
async def test_connector_ingress_runtime_routes_message_to_agent_session(tmp_path):
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=MockProvider()))
    ingress = ConnectorIngressRuntime(home=tmp_path, runtime=runtime, project_dir=tmp_path, allow_sources={"action", "core"})

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
    ingress = ConnectorIngressRuntime(home=tmp_path, runtime=runtime, project_dir=tmp_path)

    names = {spec.name for spec in ingress.agent.capabilities.planner_specs()}

    assert {
        "final.answer",
        "clarify.ask",
        "delegate.spawn",
        "delegate.prepare",
        "approval.request",
        "delegate.run",
        "watch.create",
        "approval.resolve",
        "delegate.delete",
    } <= names
    assert {"provider.config", "service.status", "delegate.status", "delegate.list"} <= names
    assert "watch.delete" not in names
    assert "file.read" not in names
    assert "file.write" not in names
    assert "filesystem.list" not in names
    assert "git.status" not in names
    assert "shell.run" not in names
    assert "test.run" not in names


def test_connector_ingress_runtime_exposes_remote_tool_policy(tmp_path):
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=MockProvider()))
    ingress = ConnectorIngressRuntime(home=tmp_path, runtime=runtime, project_dir=tmp_path)

    facts = ingress.tool_policy.facts()

    assert ingress.tool_policy == REMOTE_CONNECTOR_TOOL_POLICY
    assert facts["name"] == "remote_connector_default"
    assert facts["permission_ceiling"] == "write"
    assert "delegate.spawn" in facts["allowed_tools"]
    assert "shell" in facts["blocked_capability_classes"]
    assert "browser" in facts["blocked_capability_classes"]
    assert "filesystem" in facts["blocked_capability_classes"]
    assert "direct filesystem" in facts["reason"]


def test_connector_runtime_has_no_legacy_allowlist_alias():
    import navi.connector_runtime as connector_runtime

    assert not hasattr(connector_runtime, "CONNECTOR_ALLOWED_TOOLS")


@pytest.mark.asyncio
async def test_tools_list_reflects_connector_allowlist(tmp_path):
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=MockProvider()))
    ingress = ConnectorIngressRuntime(home=tmp_path, runtime=runtime, project_dir=tmp_path)

    result = await ingress.agent.capabilities.invoke(
        "tools.list",
        {},
        permission="read",
        context=CapabilityContext(home=tmp_path, source="connector.test", permission_ceiling="write"),
    )

    names = {item["name"] for item in result.facts["tools"]}
    by_name = {item["name"]: item for item in result.facts["tools"]}
    assert "delegate.spawn" in names
    assert "watch.create" in names
    assert "watch.delete" not in names
    assert "file.read" not in names
    assert "browser.screenshot" not in names
    assert by_name["delegate.spawn"]["safeguards"]["risk_class"] == "medium"
    assert "task_control" in by_name["delegate.spawn"]["safeguards"]["sensitive_contexts"]
    assert "scheduled_activity" in by_name["watch.create"]["safeguards"]["sensitive_contexts"]
