from __future__ import annotations

import json
import sqlite3

import pytest
import yaml

from navi.evolution import EvolutionEngine, EvolutionLedger
from navi.provider import ChatMessage, _extract_openai_content
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
