from __future__ import annotations

import pytest

from navi.agent_kernel import AgentKernel
from navi.fact_tools import ServiceFacts
from navi.provider import ChatMessage, MockProvider
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
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    router = AgentKernel(home=tmp_path, runtime=runtime, project_dir=tmp_path)

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
