from __future__ import annotations

import pytest

from navi.engine import HernessEngine
from navi.fact_tools import ServiceFacts
from navi.provider import ChatMessage, MockProvider, ModelPool
from navi.runtime import AgentRuntime
from navi.tasks import TaskStore


class ScriptedProvider(MockProvider):
    def __init__(self, responses: list[str]):
        self.responses = responses
        self.messages: list[list[ChatMessage]] = []

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.messages.append(messages)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_engine_can_chain_multiple_read_capabilities_before_answering(tmp_path, monkeypatch):
    provider = ScriptedProvider(
        [
            '{"tool":"provider.config","permission":"read","args":{},"confidence":0.9,"reason":"need provider facts"}',
            '{"tool":"service.status","permission":"read","args":{"name":"navi.service"},"confidence":0.9,"reason":"need service facts"}',
            '{"tool":"final.answer","permission":"read","args":{"message":"当前 provider 是 mock，服务状态是 active。"},"confidence":0.9,"reason":"observations are sufficient"}',
        ]
    )

    import navi.core_tools as tools_module

    monkeypatch.setattr(
        tools_module,
        "service_facts",
        lambda name: ServiceFacts(
            name=name,
            properties={"ActiveState": "active", "SubState": "running"},
            exit_code=0,
            stderr="",
        ),
    )
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=provider))
    router = HernessEngine(home=tmp_path, runtime=runtime, project_dir=tmp_path)

    result = await router.handle(
        "检查当前模型和服务状态",
        peer_id="web",
        sender_id="web",
        source="web",
    )

    assert result.action == "tool"
    assert result.text == "当前 provider 是 mock，服务状态是 active。"
    assert len(provider.messages) == 3
    assert "Observed facts in this turn:" in provider.messages[1][1].content
    assert "provider" in provider.messages[1][1].content
    assert "service.status" in provider.messages[2][1].content
    logs = TaskStore(tmp_path).list_tool_call_logs()
    assert [log.tool for log in logs[:2]] == ["service.status", "provider.config"]


@pytest.mark.asyncio
async def test_engine_budget_exhausted_with_observations(tmp_path):
    provider = ScriptedProvider(
        [
            '{"tool":"provider.config","permission":"read","args":{},"confidence":0.9,"reason":"need provider facts"}',
            'Final answer content'
        ]
    )
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=provider))
    router = HernessEngine(home=tmp_path, runtime=runtime, project_dir=tmp_path, step_budget=1)

    result = await router.handle(
        "Hello",
        peer_id="web",
        sender_id="web",
        source="web",
    )
    assert result.terminal is True
    assert "(注意：已达到步骤预算上限，任务可能未完成。)" in result.text
    assert "Warning: Step budget limit reached" in result.text


@pytest.mark.asyncio
async def test_engine_budget_exhausted_without_observations(tmp_path):
    provider = ScriptedProvider(
        [
            'Fallback chat reply'
        ]
    )
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=provider))
    router = HernessEngine(home=tmp_path, runtime=runtime, project_dir=tmp_path, step_budget=0)

    result = await router.handle(
        "Hello",
        peer_id="web",
        sender_id="web",
        source="web",
    )
    assert result.terminal is True
    assert "Fallback chat reply" in result.text
    assert "(注意：已达到步骤预算上限，任务可能未完成。)" in result.text


@pytest.mark.asyncio
async def test_engine_terminal_empty_text_records_turn(tmp_path):
    provider = ScriptedProvider(
        [
            '{"tool":"final.answer","permission":"read","args":{"message":""},"confidence":0.9,"reason":"done"}'
        ]
    )
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=provider))
    router = HernessEngine(home=tmp_path, runtime=runtime, project_dir=tmp_path)

    result = await router.handle(
        "Hello",
        peer_id="web",
        sender_id="web",
        source="web",
    )
    assert result.text == ""
    assert result.terminal is True
    
    messages = runtime.memory.get_messages(result.session_id)
    assert len(messages) == 2
    assert messages[0].content == "Hello"
    assert messages[1].content == ""


@pytest.mark.asyncio
async def test_engine_shutdown_cancels_background_tasks(tmp_path):
    provider = ScriptedProvider(
        [
            '{"tool":"final.answer","permission":"read","args":{"message":"done"},"confidence":0.9,"reason":"done"}'
        ]
    )
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=provider))
    router = HernessEngine(home=tmp_path, runtime=runtime, project_dir=tmp_path)

    import asyncio
    async def slow_consolidate(*args, **kwargs):
        await asyncio.sleep(5)
        
    runtime.memory.extract_and_consolidate_memories = slow_consolidate

    result = await router.handle(
        "Hello",
        peer_id="web",
        sender_id="web",
        source="web",
    )
    assert len(router._background_tasks) == 1
    
    await router.shutdown(timeout=0.01)
    
    await asyncio.sleep(0.05)
    assert len(router._background_tasks) == 0
