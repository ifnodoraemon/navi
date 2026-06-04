from __future__ import annotations

import json
import re

import pytest

from navi.engine import HernessEngine
from navi.defaults import DEFAULT_LOCAL_SURFACE
from navi.fact_tools import ServiceFacts
from navi.goals import GoalStore
from navi.provider import ChatMessage, MockProvider, ModelPool
from navi.runtime import AgentRuntime
from navi.runs import RunStore


class ScriptedProvider(MockProvider):
    def __init__(self, responses: list[str]):
        self.responses = responses
        self.messages: list[list[ChatMessage]] = []

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.messages.append(messages)
        response = self.responses.pop(0)
        if "TASK_ID" in response:
            for message in reversed(messages):
                match = re.search(r'"run_id":\s*"([^"]+)"', message.content)
                if match:
                    return response.replace("TASK_ID", match.group(1))
        return response


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
        peer_id=DEFAULT_LOCAL_SURFACE,
        sender_id=DEFAULT_LOCAL_SURFACE,
        source=DEFAULT_LOCAL_SURFACE,
    )

    assert result.action == "tool"
    assert result.text == "当前 provider 是 mock，服务状态是 active。"
    assert len(provider.messages) == 3
    assert "[OBSERVED FACTS]" in provider.messages[1][1].content
    assert "provider" in provider.messages[1][1].content
    assert "service.status" in provider.messages[2][1].content
    logs = RunStore(tmp_path).list_tool_call_logs()
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
        peer_id=DEFAULT_LOCAL_SURFACE,
        sender_id=DEFAULT_LOCAL_SURFACE,
        source=DEFAULT_LOCAL_SURFACE,
    )
    assert result.terminal is True
    assert result.text == "Final answer content"
    assert result.budget_exhausted is True
    assert "步骤预算" not in result.text
    assert "Step budget" not in result.text
    events = router.trace.list_events(result.trace_id)
    role_events = [event for event in events if event.phase == "agent.role_result"]
    assert role_events
    assert role_events[0].model_role == "responder"
    final_events = [event for event in events if event.phase == "turn.final"]
    assert final_events
    assert json.loads(final_events[0].output_json)["budget_exhausted"] is True


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
        peer_id=DEFAULT_LOCAL_SURFACE,
        sender_id=DEFAULT_LOCAL_SURFACE,
        source=DEFAULT_LOCAL_SURFACE,
    )
    assert result.terminal is True
    assert result.text == "Fallback chat reply"
    assert result.budget_exhausted is True
    assert "步骤预算" not in result.text
    assert "Step budget" not in result.text


@pytest.mark.asyncio
async def test_engine_budget_recovery_prepares_and_requests_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    provider = ScriptedProvider(
        [
            '{"tool":"delegate.spawn","permission":"prepare","args":{"prompt":"列一下本机目录"},"confidence":0.9,"reason":"local work"}',
            "任务已准备好，等待审批。",
        ]
    )
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=provider))
    router = HernessEngine(home=tmp_path, runtime=runtime, project_dir=tmp_path, step_budget=1)

    result = await router.handle(
        "列一下本机目录",
        peer_id=DEFAULT_LOCAL_SURFACE,
        sender_id=DEFAULT_LOCAL_SURFACE,
        source=DEFAULT_LOCAL_SURFACE,
    )

    task = RunStore(tmp_path).list()[0]
    assert task.status == "awaiting_approval"
    assert result.budget_exhausted is True
    assert result.text.startswith("任务已准备好，等待审批。")
    assert "审批码:" in result.text
    events = router.trace.list_events(result.trace_id)
    assert any(event.phase == "runtime.budget_exhausted" for event in events)
    recovered_tools = [
        event.tool
        for event in events
        if event.phase == "capability.result" and event.model_role == "runtime"
    ]
    assert recovered_tools == ["delegate.prepare", "approval.request"]


@pytest.mark.asyncio
async def test_engine_blocks_final_answer_when_recorded_task_is_still_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    provider = ScriptedProvider(
        [
            '{"tool":"delegate.spawn","permission":"prepare","args":{"prompt":"列一下我本机的目录"},"confidence":0.9,"reason":"local work"}',
            '{"tool":"final.answer","permission":"read","args":{"message":"已完成。"},"confidence":0.9,"reason":"premature"}',
            '{"tool":"delegate.prepare","permission":"prepare","args":{"run_id":"TASK_ID"},"confidence":0.9,"reason":"prepare pending task"}',
            '{"tool":"approval.request","permission":"prepare","args":{"run_id":"TASK_ID"},"confidence":0.9,"reason":"request approval"}',
            '{"tool":"final.answer","permission":"read","args":{"message":"任务已准备好，等待审批。"},"confidence":0.9,"reason":"approval is pending"}',
        ]
    )
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=provider))
    router = HernessEngine(home=tmp_path, runtime=runtime, project_dir=tmp_path)

    result = await router.handle(
        "列一下我本机的目录",
        peer_id=DEFAULT_LOCAL_SURFACE,
        sender_id=DEFAULT_LOCAL_SURFACE,
        source=DEFAULT_LOCAL_SURFACE,
    )

    task = RunStore(tmp_path).list()[0]
    assert task.status == "awaiting_approval"
    goal = GoalStore(tmp_path).get_by_run(task.id)
    assert goal is not None
    assert goal.trace_id == result.trace_id
    assert goal.session_id == result.session_id
    assert result.text.startswith("任务已准备好，等待审批。")
    assert "审批码:" in result.text
    assert len(provider.messages) == 5
    assert "Recovery plan:" in provider.messages[2][1].content
    trace_events = router.trace.list_events(result.trace_id)
    assert any(event.phase == "completion.verify" and not event.ok for event in trace_events)
    recovery_events = [event for event in trace_events if event.phase == "recovery.plan"]
    assert recovery_events
    assert recovery_events[0].message == "continue"


@pytest.mark.asyncio
async def test_engine_blocks_final_answer_after_partial_failed_task_cleanup(tmp_path):
    store = RunStore(tmp_path)
    for index in range(3):
        store.create(f"failed cleanup {index}", status="failed", source="watch", kind="delegation", workspace=str(tmp_path))
    provider = ScriptedProvider(
        [
            '{"tool":"delegate.delete","permission":"write","args":{"status":"failed","source":"watch","limit":1},"confidence":0.9,"reason":"cleanup failed tasks"}',
            '{"tool":"final.answer","permission":"read","args":{"message":"清理完成。"},"confidence":0.9,"reason":"premature"}',
            '{"tool":"delegate.delete","permission":"write","args":{"status":"failed","source":"watch"},"confidence":0.9,"reason":"finish cleanup"}',
            '{"tool":"final.answer","permission":"read","args":{"message":"失败任务已清理完毕。"},"confidence":0.9,"reason":"verified complete"}',
        ]
    )
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=provider))
    router = HernessEngine(home=tmp_path, runtime=runtime, project_dir=tmp_path)

    result = await router.handle(
        "清理失败任务",
        peer_id=DEFAULT_LOCAL_SURFACE,
        sender_id=DEFAULT_LOCAL_SURFACE,
        source=DEFAULT_LOCAL_SURFACE,
    )

    assert store.count_runs(status="failed", source="watch") == 0
    assert result.text == "失败任务已清理完毕。"
    assert len(provider.messages) == 4
    assert "Recovery plan:" in provider.messages[2][1].content
    trace_events = router.trace.list_events(result.trace_id)
    assert any(event.phase == "completion.verify" and not event.ok for event in trace_events)
    recovery_events = [event for event in trace_events if event.phase == "recovery.plan"]
    assert recovery_events
    assert recovery_events[0].message == "continue"


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
        peer_id=DEFAULT_LOCAL_SURFACE,
        sender_id=DEFAULT_LOCAL_SURFACE,
        source=DEFAULT_LOCAL_SURFACE,
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

    await router.handle(
        "Hello",
        peer_id=DEFAULT_LOCAL_SURFACE,
        sender_id=DEFAULT_LOCAL_SURFACE,
        source=DEFAULT_LOCAL_SURFACE,
    )
    assert len(router._background_tasks) == 1
    
    await router.shutdown(timeout=0.01)
    
    await asyncio.sleep(0.05)
    assert len(router._background_tasks) == 0


def test_approval_prompt_uses_surface_affordance():
    facts = {
        "run_id": "task-1",
        "status": "awaiting_approval",
        "approval": {"code": "123456", "expires_at": 0},
    }

    telegram = HernessEngine._approval_prompt_from_facts(facts, source="telegram")
    default = HernessEngine._approval_prompt_from_facts(facts, source=DEFAULT_LOCAL_SURFACE)

    assert "Approval code" in telegram
    assert "Reply `approve 123456`" in telegram
    assert "审批码: `123456`" in default
    assert "批准 123456" in default


@pytest.mark.asyncio
async def test_engine_records_full_flow_trace_and_evaluation(tmp_path):
    from navi.trace import TraceStore

    provider = ScriptedProvider(
        [
            '{"tool":"final.answer","permission":"read","args":{"message":"done"},"confidence":0.9,"reason":"direct answer"}'
        ]
    )
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=provider))
    router = HernessEngine(home=tmp_path, runtime=runtime, project_dir=tmp_path)

    result = await router.handle(
        "Hello",
        peer_id=DEFAULT_LOCAL_SURFACE,
        sender_id=DEFAULT_LOCAL_SURFACE,
        source=DEFAULT_LOCAL_SURFACE,
    )
    await router.shutdown(timeout=0.01)

    trace = TraceStore(tmp_path)
    events = trace.list_events(result.trace_id)
    phases = [event.phase for event in events]
    evaluation = trace.evaluate_trace(result.trace_id)

    assert result.trace_id
    assert phases == ["turn.start", "planner.syscall", "capability.result", "turn.final"]
    assert events[1].tool == "final.answer"
    assert evaluation.outcome == "success"
    assert evaluation.failure_domain == "none"
