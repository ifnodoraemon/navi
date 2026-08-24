from __future__ import annotations

import json
import re

import pytest

from navi.finalization import (
    synthesize_background_notification,
    synthesize_user_reply_from_facts,
)


class _FailingRuntime:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, role: str) -> str:
        del messages
        self.calls += 1
        assert role == "responder"
        raise RuntimeError("responder unavailable")


class _CapturingRuntime:
    def __init__(self) -> None:
        self.messages = []

    async def complete(self, messages, *, role: str) -> str:
        assert role == "responder"
        self.messages = messages
        return "已整理。"


class _NotificationProvider:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.messages = []

    async def complete_for(self, role, messages, *, output_schema):
        assert role == "notification"
        assert messages
        assert output_schema
        self.messages = messages
        return json.dumps(self.response, ensure_ascii=False)


class _NotificationRuntime:
    def __init__(self, response: dict[str, object]) -> None:
        self.provider = _NotificationProvider(response)


@pytest.mark.asyncio
async def test_fact_response_propagates_model_failure_without_empty_text_substitution():
    runtime = _FailingRuntime()

    with pytest.raises(RuntimeError, match="responder unavailable"):
        await synthesize_user_reply_from_facts(
            runtime,
            user_text="what happened",
            facts={"state_transition": "failed"},
        )

    assert runtime.calls == 1


@pytest.mark.asyncio
async def test_fact_response_projects_large_capability_evidence_before_model_call():
    runtime = _CapturingRuntime()

    assert (
        await synthesize_user_reply_from_facts(
            runtime,
            user_text="总结结果",
            facts={"capability_result": {"facts": {"content": "x" * 300_000}}},
        )
        == "已整理。"
    )

    rendered = runtime.messages[-1].content
    match = re.search(
        r"<verified_facts>\s*<!\[CDATA\[(.*?)\]\]>\s*</verified_facts>",
        rendered,
        re.DOTALL,
    )
    assert match is not None
    facts = json.loads(match.group(1))
    assert "[truncated" in facts["capability_result"]["facts"]["content"]


@pytest.mark.asyncio
async def test_background_notification_receives_typed_calendar_facts():
    runtime = _NotificationRuntime(
        {
            "notify": True,
            "message": "计划执行时间是 2026-07-30 13:00:00。",
        }
    )

    decision = await synthesize_background_notification(
        runtime,
        facts={"scheduled_for_iso": "2026-07-30T13:00:00+08:00"},
        output_schema={"name": "background_notification"},
    )

    assert decision.notify is True
    assert decision.message == "计划执行时间是 2026-07-30 13:00:00。"
    assert "2026-07-30T13:00:00+08:00" in runtime.provider.messages[-1].content
