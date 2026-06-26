from __future__ import annotations

import json
import sqlite3

import pytest
import yaml

from navi.engine import HernessEngine
from navi.evolution import EvolutionEngine, EvolutionLedger
from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.engine_types import AgentTurnResult
from navi.execution import ExecutionService
from navi.provider import ChatMessage, _extract_anthropic_content, _extract_openai_content
from navi.runtime import AgentRuntime
from navi.runs import RunStore
from navi.syscalls import ModelSyscallPlanner
from navi.trace import TraceStore


def test_evolution_ledger_uses_latest_run_id_schema(tmp_path):
    EvolutionLedger(tmp_path)

    with sqlite3.connect(tmp_path / "evolution.db") as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(evolution_events)").fetchall()}

    assert "run_id" in columns
    assert "task_id" not in columns


def test_provider_recovers_structured_json_from_reasoning_content():
    data = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "reasoning_content": (
                        "reasoning omitted\nresponse"
                        '{"tool":"final.answer","permission":"read","args":{"message":"ok"}}'
                    ),
                },
                "finish_reason": "stop",
            }
        ]
    }

    recovered = json.loads(_extract_openai_content(data))

    assert recovered["tool"] == "final.answer"
    assert recovered["args"]["message"] == "ok"


class _PlannerSchemaProvider:
    def __init__(self) -> None:
        self.output_schema: dict | None = None

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        *,
        output_schema: dict | None = None,
    ) -> str:
        self.output_schema = output_schema
        return json.dumps(
            {
                "tool": "final.answer",
                "permission": "read",
                "args": {"message": "ok"},
                "model_role": "responder",
                "confidence": 1.0,
                "reason": "facts available",
            }
        )


@pytest.mark.asyncio
async def test_planner_structured_output_wrapper_is_not_a_capability_name():
    provider = _PlannerSchemaProvider()
    planner = ModelSyscallPlanner(provider)

    decision = await planner.plan("hi", tools=[])

    assert provider.output_schema["name"] == "planner_decision"
    assert decision.tool == "final.answer"


def test_anthropic_structured_wrapper_returns_inner_planner_decision():
    raw = {
        "content": [
            {
                "type": "tool_use",
                "name": "planner_decision",
                "input": {
                    "tool": "delegate.list",
                    "permission": "read",
                    "args": {"limit": 10},
                    "model_role": "planner",
                    "confidence": 1,
                    "reason": "inspect run facts",
                },
            }
        ]
    }

    parsed = json.loads(_extract_anthropic_content(raw, tool_name="planner_decision"))

    assert parsed["tool"] == "delegate.list"


class _StructuredJourneyProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        *,
        output_schema: dict | None = None,
    ) -> str:
        self.calls.append(
            {
                "role": role,
                "messages": messages,
                "output_schema": output_schema,
            }
        )
        return json.dumps(
            {
                "id": "repo_review_history",
                "user_goal": "Review repository principles after code changes",
                "steps": [
                    {
                        "user": "再次全面审查，只列问题",
                        "expect": {"text_contains": "问题"},
                    }
                ],
            }
        )


class _DeleteExpiredProvider:
    def __init__(self) -> None:
        self.planner_calls = 0
        self.responder_calls = 0

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        *,
        output_schema: dict | None = None,
    ) -> str:
        if role == "planner":
            self.planner_calls += 1
            return json.dumps(
                {
                    "tool": "delegate.delete",
                    "permission": "write",
                    "args": {
                        "status": "expired",
                        "kind": "delegation",
                        "reason": "delete expired tasks",
                    },
                    "reason": "clean up expired delegation runs",
                }
            )
        if role == "responder":
            self.responder_calls += 1
            content = "\n".join(message.content for message in messages)
            assert '"cleanup_complete": true' in content
            assert '"deleted_count": 1' in content
            return "已删除 1 个过期任务。"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]


class _RepeatListProvider:
    def __init__(self) -> None:
        self.planner_calls = 0
        self.responder_calls = 0

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        *,
        output_schema: dict | None = None,
    ) -> str:
        if role == "planner":
            self.planner_calls += 1
            return json.dumps(
                {
                    "tool": "delegate.list",
                    "permission": "read",
                    "args": {"limit": 20},
                    "reason": "inspect current tasks",
                }
            )
        if role == "responder":
            self.responder_calls += 1
            content = "\n".join(message.content for message in messages)
            assert "Runtime convergence" not in content
            assert "Capability observations:" in content
            return "当前没有任务。"
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]


@pytest.mark.asyncio
async def test_expired_task_cleanup_finishes_from_completion_facts(tmp_path):
    runs = RunStore(tmp_path)
    expired = runs.create(
        "在用户电脑上找到简历文件并发送给用户",
        kind="delegation",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        status="expired",
    )
    provider = _DeleteExpiredProvider()
    engine = HernessEngine(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        permission_ceiling="write",
    )

    result = await engine.handle(
        "删除过期的任务",
        peer_id="peer-1",
        sender_id="sender-1",
        source="weixin",
        session_alias="weixin:peer-1:sender-1",
    )

    assert result.text == "已删除 1 个过期任务。"
    assert result.terminal is True
    assert runs.get(expired.id) is None
    assert provider.planner_calls == 1
    assert provider.responder_calls == 1
    phases = [event.phase for event in TraceStore(tmp_path).list_events(result.trace_id)]
    assert "runtime.converged" not in phases


@pytest.mark.asyncio
async def test_repeated_stable_capability_result_converges(tmp_path):
    provider = _RepeatListProvider()
    engine = HernessEngine(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        permission_ceiling="write",
    )

    result = await engine.handle(
        "我们现在都要哪些任务",
        peer_id="peer-1",
        sender_id="sender-1",
        source="weixin",
        session_alias="weixin:peer-1:sender-1",
    )

    assert result.text == "当前没有任务。"
    assert provider.planner_calls == 2
    assert provider.responder_calls == 1
    phases = [event.phase for event in TraceStore(tmp_path).list_events(result.trace_id)]
    assert "runtime.converged" in phases


@pytest.mark.asyncio
async def test_evolution_engine_extracts_daily_eval_from_session_trace(tmp_path):
    home = tmp_path / "home"
    trace = TraceStore(home)
    trace_id = "trace-1"
    session_id = "session-1"
    run_id = "run-1"
    trace.add_event(
        trace_id=trace_id,
        session_id=session_id,
        run_id=run_id,
        phase="turn.start",
        input_data={"message": "再次全面审查，只列问题"},
    )
    trace.add_event(
        trace_id=trace_id,
        session_id=session_id,
        run_id=run_id,
        phase="planner.syscall",
        tool="tools.list",
        output_data={"tool": "tools.list", "reason": "collect facts first"},
    )
    trace.add_event(
        trace_id=trace_id,
        session_id=session_id,
        run_id=run_id,
        phase="capability.result",
        tool="tools.list",
        message="42 tools available",
    )
    trace.add_event(
        trace_id=trace_id,
        session_id=session_id,
        run_id=run_id,
        phase="turn.final",
        message="问题：无",
    )

    provider = _StructuredJourneyProvider()
    engine = EvolutionEngine(home)
    engine.provider = provider

    await engine.extract_evals_from_session(session_id, run_id=run_id)

    assert provider.calls
    call = provider.calls[0]
    assert call["role"] == "planner"
    assert call["output_schema"]["name"] == "daily_journey_eval"
    assert "tools.list" in call["messages"][0].content

    data = yaml.safe_load((tmp_path / "evals" / "auto_captured_journeys.yaml").read_text())
    assert data["journeys"][0]["id"] == "repo_review_history"
