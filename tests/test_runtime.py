from __future__ import annotations

import pytest

from navi.provider import ChatMessage, MockProvider
from navi.runtime import AgentRuntime
from navi.operating_context import OperatingContext


class RecordingProvider(MockProvider):
    def __init__(self):
        self.messages: list[ChatMessage] = []

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.messages = messages
        return await super().complete(messages)


@pytest.mark.asyncio
async def test_runtime_persists_session_messages(tmp_path):
    runtime = AgentRuntime(home=tmp_path, provider=MockProvider())

    reply = await runtime.chat("hello")

    assert reply.session_id
    assert reply.content == "Navi received: hello"
    messages = runtime.memory.get_messages(reply.session_id)
    assert [message.role for message in messages] == ["user", "assistant"]


def test_memory_append_and_read(tmp_path):
    runtime = AgentRuntime(home=tmp_path, provider=MockProvider())

    runtime.memory.append_memory("Prefers concise replies")

    assert "Prefers concise replies" in runtime.memory.read_memory()


def test_memory_recall_prioritizes_constraints_and_relevance(tmp_path):
    runtime = AgentRuntime(home=tmp_path, provider=MockProvider())
    runtime.memory.add_item(
        "constraint",
        "Never run destructive git commands without explicit approval.",
        source="test",
        status="active",
        confidence=1.0,
    )
    runtime.memory.add_item(
        "fact",
        "The Navi project lives under /home/ifnodoraemon/myagent/navi.",
        source="test",
        status="active",
        confidence=0.9,
        scope="project:navi",
    )
    runtime.memory.add_item(
        "fact",
        "The unrelated cooking notebook uses grams.",
        source="test",
        status="active",
        confidence=0.9,
        scope="project:cooking",
    )

    rendered = runtime.memory.render_context("inspect navi project git status")

    assert "Never run destructive git commands" in rendered
    assert "Navi project lives" in rendered
    assert "cooking notebook" not in rendered


def test_session_alias_rotation_preserves_old_messages(tmp_path):
    runtime = AgentRuntime(home=tmp_path, provider=MockProvider())
    first = runtime.memory.current_session_id("connector:test:peer")
    runtime.memory.add_message(first, "user", "old topic")

    second = runtime.memory.rotate_session("connector:test:peer").session_id
    runtime.memory.add_message(second, "user", "new topic")

    assert first != second
    assert runtime.memory.current_session_id("connector:test:peer") == second
    assert runtime.memory.get_messages(first)[0].content == "old topic"
    assert runtime.memory.get_messages(second)[0].content == "new topic"


@pytest.mark.asyncio
async def test_runtime_system_prompt_includes_local_deployment_contract(tmp_path):
    provider = RecordingProvider()
    runtime = AgentRuntime(home=tmp_path, provider=provider)

    await runtime.chat("列一下我本机的目录")

    system = provider.messages[0].content
    assert "running on their own machine" in system
    assert "Current workspace:" in system
    assert "Navi state home:" in system
    assert "Local execution bridge" in system
    assert "Web console URL: not configured" in system
    assert "Do not say you have no access to the user's local machine as an absolute statement" in system
    assert "Do not frame local actions as a generic permission failure" in system
    assert "this chat response itself is not a shell" in system
    assert "Do not give a CLI invocation for task creation" in system
    assert "user input for the kernel syscall planner" in system
    assert "Do not invent product surfaces" in system
    assert "127.0.0.1:8765" not in system
    assert "Current conversational channel" not in system
    assert "Remote connectors:" not in system
    assert "active surface" not in system
    assert "Weixin" not in system


@pytest.mark.asyncio
async def test_runtime_system_prompt_uses_configured_web_url(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEB_URL", "http://navi.local")
    provider = RecordingProvider()
    runtime = AgentRuntime(home=tmp_path, provider=provider)

    await runtime.chat("hello")

    system = provider.messages[0].content
    assert "Web console URL: http://navi.local" in system


@pytest.mark.asyncio
async def test_runtime_system_prompt_uses_goal_directed_memory(tmp_path):
    provider = RecordingProvider()
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    runtime.memory.add_item(
        "constraint",
        "Do not forget approval state during long context.",
        source="test",
        status="active",
        confidence=1.0,
    )
    runtime.memory.add_item(
        "fact",
        "The Navi deployment uses systemd user service.",
        source="test",
        status="active",
        confidence=0.9,
    )
    runtime.memory.add_item(
        "fact",
        "The unrelated archive is stored on cold media.",
        source="test",
        status="active",
        confidence=0.9,
    )

    await runtime.chat("检查 navi deployment service 状态")

    system = provider.messages[0].content
    assert "Memory recall:" in system
    assert "approval state" in system
    assert "systemd user service" in system
    assert "cold media" not in system


@pytest.mark.asyncio
async def test_runtime_prompt_layers_and_skill_permissions_are_scoped(tmp_path):
    read_skill = tmp_path / "skills" / "read-skill"
    read_skill.mkdir(parents=True)
    read_skill.joinpath("SKILL.md").write_text(
        "---\nname: read-skill\npermission: read\n---\nRead layer skill body.",
        encoding="utf-8",
    )
    write_skill = tmp_path / "skills" / "write-skill"
    write_skill.mkdir(parents=True)
    write_skill.joinpath("SKILL.md").write_text(
        "---\nname: write-skill\npermission: write\n---\nWrite layer skill body.",
        encoding="utf-8",
    )
    provider = RecordingProvider()
    runtime = AgentRuntime(home=tmp_path, provider=provider)

    await runtime.chat(
        "hello",
        operating_context=OperatingContext(
            home=tmp_path,
            permission_ceiling="read",
            skill_permission_ceiling="read",
            prompt_layers=("identity", "runtime", "skills"),
        ),
    )

    system = provider.messages[0].content
    assert "[identity]" in system
    assert "[runtime]" in system
    assert "[skills]" in system
    assert "[authorization]" not in system
    assert "Permission ceiling: read" in system
    assert "Read layer skill body." in system
    assert "Write layer skill body." not in system
