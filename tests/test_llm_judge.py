"""Tests for LLM Meta-Cognitive Judge and RLHF/RLAIF Evaluator."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from navi.credit_assignment import CreditAssignmentEngine
from navi.llm_judge import (
    LLMJudge,
    LLMJudgeEvaluation,
    _extract_judge_json,
)
from navi.loop import TraceFailureDomain
from navi.replay_buffer import ExperienceReplayBuffer
from navi.trace import TraceStore
from navi.turn_lifecycle import AgentTurnResult, TurnLifecycleMixin


class DummyProvider:
    """Mock LLM provider returning predetermined JSON responses."""

    def __init__(self, response_text: str):
        self.response_text = response_text

    async def complete_for(self, role: str, messages: list[Any]) -> str:
        del role, messages
        return self.response_text


def test_extract_judge_json() -> None:
    # 1. Empty string
    assert _extract_judge_json("") == {}
    assert _extract_judge_json("   ") == {}

    # 2. Markdown fenced json
    fenced = "```json\n{\"reward\": 0.85, \"verdict\": \"positive_reinforcement\"}\n```"
    res1 = _extract_judge_json(fenced)
    assert res1.get("reward") == 0.85
    assert res1.get("verdict") == "positive_reinforcement"

    # 3. Markdown triple-backticks without json tag
    plain_fenced = "```\n{\"reward\": -0.5, \"verdict\": \"correction_needed\"}\n```"
    res2 = _extract_judge_json(plain_fenced)
    assert res2.get("reward") == -0.5

    # 4. Embedded in conversational text
    prose = "Here is my meta evaluation:\n{\"reward\": 0.1, \"verdict\": \"neutral_continuation\"}\nHope this helps!"
    res3 = _extract_judge_json(prose)
    assert res3.get("reward") == 0.1

    # 5. Invalid json
    assert _extract_judge_json("not valid json at all") == {}


async def test_llm_judge_evaluate_async_positive(tmp_path: Path) -> None:
    judge = LLMJudge(tmp_path)
    mock_resp = """```json
{
  "reward": 0.90,
  "verdict": "positive_reinforcement",
  "failure_domain": "none",
  "confidence": 0.95,
  "reasoning": "User confirmed task success with praise"
}
```"""
    provider = DummyProvider(mock_resp)
    evaluation = await judge.evaluate_async(
        trace_id="tr_pos_1",
        session_id="s1",
        user_prompt="Can you summarize this file?",
        assistant_response="Here is the summary...",
        followup_feedback="Excellent, exactly what I needed!",
        provider=provider,
        now=1000.0,
    )

    assert evaluation is not None
    assert evaluation.trace_id == "tr_pos_1"
    assert evaluation.reward == 0.90
    assert evaluation.verdict == "positive_reinforcement"
    assert evaluation.failure_domain == "none"
    assert evaluation.confidence == 0.95
    assert "praise" in evaluation.reasoning

    # Test retrieval from database
    retrieved = judge.get_evaluation("tr_pos_1")
    assert retrieved is not None
    assert retrieved.trace_id == "tr_pos_1"
    assert retrieved.reward == 0.90

    # Test list_evaluations
    all_evals = judge.list_evaluations(limit=10)
    assert len(all_evals) == 1
    assert all_evals[0].trace_id == "tr_pos_1"


def test_llm_judge_evaluate_sync(tmp_path: Path) -> None:
    judge = LLMJudge(tmp_path)
    mock_resp = '{"reward": 0.3, "verdict": "neutral_continuation", "failure_domain": "none", "confidence": 0.8, "reasoning": "Clarification"}'
    provider = DummyProvider(mock_resp)

    evaluation = judge.evaluate(
        trace_id="tr_sync_1",
        session_id="s2",
        user_prompt="Next step?",
        assistant_response="Proceed to step 2",
        provider=provider,
    )
    assert evaluation is not None
    assert evaluation.reward == 0.3


def test_llm_judge_apply_judgment_to_buffer_and_credit(tmp_path: Path) -> None:
    judge = LLMJudge(tmp_path)
    replay_buf = ExperienceReplayBuffer(tmp_path)
    trace_store = TraceStore(tmp_path)

    # 1. Setup trace and replay buffer entry
    trace_id = "tr_fail_1"
    trace_store.new_trace_id()
    trace_store.add_event(
        trace_id=trace_id,
        phase="planner.start",
        message="Running plan",
    )

    replay_buf.record_experience(
        trace_id=trace_id,
        channel="cli",
        prompt="Delete the temp folder",
        response="Deleted everything including production data",
        reward=0.0,
        metadata={},
    )

    # 2. Evaluation with severe failure
    eval_fail = LLMJudgeEvaluation(
        id="eval_1",
        trace_id=trace_id,
        session_id="s1",
        reward=-0.80,
        verdict="critical_failure",
        failure_domain=str(TraceFailureDomain.SAFEGUARD_POLICY),
        confidence=0.99,
        reasoning="Critical data safety violation",
        user_prompt="Delete the temp folder",
        assistant_response="Deleted everything including production data",
        followup_feedback="You destroyed production files!",
        created_at=1000.0,
    )

    # 3. Apply judgment
    judge.apply_judgment(eval_fail)

    # 4. Check replay buffer updated
    updated_entries = replay_buf.get_hard_negatives(limit=5)
    assert len(updated_entries) >= 1
    assert updated_entries[0].trace_id == trace_id
    assert updated_entries[0].reward == -0.80
    assert "llm_judge" in updated_entries[0].metadata

    # 5. Check credit assignment backpropagated
    credit_engine = CreditAssignmentEngine(tmp_path)
    attribs = credit_engine.list_attributions(trace_id=trace_id)
    assert len(attribs) > 0
    assert attribs[0].failure_domain == str(TraceFailureDomain.SAFEGUARD_POLICY)


def test_credit_assignment_with_misunderstanding(tmp_path: Path) -> None:
    engine = CreditAssignmentEngine(tmp_path)
    trace_id = "tr_misund_1"
    events = [
        MagicMock(tool="", ok=True),
    ]
    attribs = engine.backprop_trace(
        trace_id=trace_id,
        outcome="failure",
        failure_domain=str(TraceFailureDomain.MISUNDERSTANDING),
        events=events,
        custom_reward=-0.65,
    )
    prompt_attribs = [a for a in attribs if a.node_type == "prompt_layer"]
    assert len(prompt_attribs) == 1
    assert prompt_attribs[0].target_id == "instructions"
    assert prompt_attribs[0].reward == -0.65


class MockTurnController(TurnLifecycleMixin):
    """Mock controller verifying background judge dispatch."""

    def __init__(self, home: Path):
        self.home = home
        self.trace = MagicMock()
        self.runtime = MagicMock()
        self.capabilities = MagicMock()
        self.permission_ceiling = "write"
        self.event_bus = None
        self.governed_run_id = ""
        self._background_tasks = set()


async def test_turn_lifecycle_prior_assistant_feedback(tmp_path: Path) -> None:
    ctrl = MockTurnController(tmp_path)

    # Mock messages in memory: prior user prompt and prior assistant reply with trace_id
    mock_msg_user = MagicMock(role="user", content="How do I sort a list?", trace_id="")
    mock_msg_asst = MagicMock(role="assistant", content="Use sort() method", trace_id="tr_prev_42")
    ctrl.runtime.memory.get_messages.return_value = [mock_msg_user, mock_msg_asst]

    with patch.object(ctrl, "_trigger_background_llm_judge") as mock_trigger:
        ctrl._evaluate_prior_assistant_feedback("session_123", "Thanks, that works!")
        mock_trigger.assert_called_once_with(
            trace_id="tr_prev_42",
            session_id="session_123",
            user_prompt="How do I sort a list?",
            assistant_response="Use sort() method",
            followup_feedback="Thanks, that works!",
        )
