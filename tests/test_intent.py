from __future__ import annotations

import pytest

from navi.intent import AgenticActionSelector
from navi.provider import ChatMessage
from navi.tools import build_core_tool_registry


class ScriptedProvider:
    def __init__(self, response: str):
        self.response = response
        self.messages: list[ChatMessage] = []

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.messages = messages
        return self.response


@pytest.mark.asyncio
async def test_agentic_selector_asks_when_schedule_time_is_vague(tmp_path):
    provider = ScriptedProvider(
        '{"kind":"ask","message":"你希望每天晚上几点上通识课？","confidence":0.92,"reason":"recurring request has vague time"}'
    )
    selector = AgenticActionSelector(provider)
    tools = build_core_tool_registry(tmp_path, project_dir=tmp_path).list_specs()

    decision = await selector.select("每天晚上上一个通识课给我", tools=tools)

    assert decision.kind == "ask"
    assert "几点" in decision.message
    system = provider.messages[0].content
    assert "Do not invent default times" in system
    assert "do not tell the user to type slash commands" in system


@pytest.mark.asyncio
async def test_agentic_selector_parses_watch_decision(tmp_path):
    provider = ScriptedProvider(
        '{"kind":"watch","prompt":"上一个通识课给我","cron":"0 21 * * *","confidence":0.95,"reason":"exact time provided"}'
    )
    selector = AgenticActionSelector(provider)
    tools = build_core_tool_registry(tmp_path, project_dir=tmp_path).list_specs()

    decision = await selector.select("每天 21 点上一个通识课给我", tools=tools)

    assert decision.kind == "watch"
    assert decision.cron == "0 21 * * *"
    assert decision.prompt == "上一个通识课给我"
