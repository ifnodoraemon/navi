"""LLM Meta-Cognitive Judge and Evaluative Critic.

Evaluates conversation turns, intent satisfaction, and follow-up user feedback
purely via semantic reasoning by an LLM (RLHF/RLAIF evaluator analog).
Zero hardcoded keywords or regex heuristics.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import time
from typing import Any
import uuid

from .db import connect
from .paths import db_paths
from .provider import ChatMessage
from .schema import Column, Table, assert_schema_exact

logger = logging.getLogger("navi.llm_judge")

LLM_JUDGE_EVALUATIONS_TABLE = Table(
    "llm_judge_evaluations",
    [
        Column("id", "TEXT", primary_key=True),
        Column("trace_id", "TEXT", nullable=False),
        Column("session_id", "TEXT", nullable=False),
        Column("reward", "REAL", nullable=False),
        Column("verdict", "TEXT", nullable=False),
        Column("failure_domain", "TEXT", nullable=False),
        Column("confidence", "REAL", nullable=False),
        Column("reasoning", "TEXT", nullable=False),
        Column("user_prompt", "TEXT", nullable=False),
        Column("assistant_response", "TEXT", nullable=False),
        Column("followup_feedback", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
    ],
)


@dataclass(frozen=True)
class LLMJudgeEvaluation:
    id: str
    trace_id: str
    session_id: str
    reward: float
    verdict: str
    failure_domain: str
    confidence: float
    reasoning: str
    user_prompt: str
    assistant_response: str
    followup_feedback: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "reward": self.reward,
            "verdict": self.verdict,
            "failure_domain": self.failure_domain,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "user_prompt": self.user_prompt,
            "assistant_response": self.assistant_response,
            "followup_feedback": self.followup_feedback,
            "created_at": self.created_at,
        }


LLM_JUDGE_SYSTEM_PROMPT = """You are an expert meta-cognitive evaluator and objective reward critic for an autonomous AI agent.
Your mission is to evaluate the interaction quality and user satisfaction entirely through deep semantic reasoning.

Evaluation Principles:
- Analyze the semantic meaning, intent alignment, pragmatic satisfaction, and emotional subtext of the user.
- If the user provides a follow-up response, examine whether it confirms success, expresses gratitude/praise, or reveals a correction, misunderstanding, complaint, or bug.
- Do NOT use keyword matching or brittle rules. Reason about the full contextual meaning of the exchange.
- Scalar reward MUST be a float strictly between -1.0 and +1.0:
    +0.80 to +1.00: Outstanding success, user delighted, or explicit high praise.
    +0.20 to +0.79: Standard successful execution, task completed accurately.
    -0.19 to +0.19: Neutral continuation, clarifying query, or minor ambiguity.
    -0.50 to -0.20: User pointed out a mistake, inaccurate answer, missing constraint, or needed correction.
    -1.00 to -0.51: Severe failure, dangerous hallucination, broken contract, data loss, or user strongly frustrated.
- Verdict must be one of: ["positive_reinforcement", "neutral_continuation", "correction_needed", "critical_failure"].
- Failure domain must be one of: ["none", "planner_or_parser", "safeguard_policy", "capability_failure", "loop_no_progress", "checker_blocked", "misunderstanding"].

Respond ONLY with a single valid JSON object with keys:
{
  "reward": float,
  "verdict": str,
  "failure_domain": str,
  "confidence": float,
  "reasoning": str
}"""


def _extract_judge_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    if not raw:
        return {}
    if "```json" in raw:
        match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
        if match:
            raw = match.group(1).strip()
    if "```" in raw:
        match = re.search(r"```\s*(.*?)\s*```", raw, re.DOTALL)
        if match:
            raw = match.group(1).strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


class LLMJudge:
    """Semantic evaluator judging user satisfaction and attributing failure domains."""

    def __init__(self, home: Path):
        self.home = home
        self.db_path = db_paths(home).traces
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(LLM_JUDGE_EVALUATIONS_TABLE.ddl)
            assert_schema_exact(conn, LLM_JUDGE_EVALUATIONS_TABLE)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_judge_trace "
                "ON llm_judge_evaluations(trace_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_judge_session "
                "ON llm_judge_evaluations(session_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_judge_reward "
                "ON llm_judge_evaluations(reward)"
            )

    async def evaluate_async(
        self,
        *,
        trace_id: str,
        session_id: str = "",
        user_prompt: str,
        assistant_response: str,
        followup_feedback: str = "",
        events_summary: str = "",
        provider: Any | None = None,
        now: float | None = None,
    ) -> LLMJudgeEvaluation | None:
        used_provider = provider
        if used_provider is None:
            try:
                from .config import load_config
                from .provider import build_provider

                config = load_config(self.home)
                used_provider = build_provider(config.model)
            except Exception as exc:
                logger.warning("LLM judge could not instantiate provider: %s", exc)
                return None

        user_content_parts = [
            f"=== User Request ===\n{user_prompt}\n",
            f"=== Assistant Response ===\n{assistant_response}\n",
        ]
        if followup_feedback:
            user_content_parts.append(f"=== User Follow-up Feedback ===\n{followup_feedback}\n")
        if events_summary:
            user_content_parts.append(f"=== Tool Execution Context ===\n{events_summary}\n")
        user_content_parts.append("Evaluate this interaction semantically and output JSON:")

        messages = [
            ChatMessage(role="system", content=LLM_JUDGE_SYSTEM_PROMPT),
            ChatMessage(role="user", content="\n".join(user_content_parts)),
        ]

        raw_output = ""
        try:
            raw_output = await used_provider.complete_for("evaluator", messages)
        except Exception:
            try:
                raw_output = await used_provider.complete_for("default", messages)
            except Exception as call_err:
                logger.warning("LLM judge completion failed: %s", call_err)
                return None

        parsed = _extract_judge_json(str(raw_output))
        if not parsed:
            return None

        raw_reward = float(parsed.get("reward", 0.0))
        clamped_reward = round(max(-1.0, min(1.0, raw_reward)), 4)
        verdict = str(parsed.get("verdict", "neutral_continuation")).strip()
        failure_domain = str(parsed.get("failure_domain", "none")).strip()
        raw_conf = float(parsed.get("confidence", 0.8))
        clamped_conf = round(max(0.0, min(1.0, raw_conf)), 4)
        reasoning = str(parsed.get("reasoning", "")).strip()

        current_time = time.time()
        if now is not None:
            current_time = float(now)

        evaluation = LLMJudgeEvaluation(
            id=uuid.uuid4().hex,
            trace_id=trace_id,
            session_id=session_id,
            reward=clamped_reward,
            verdict=verdict,
            failure_domain=failure_domain,
            confidence=clamped_conf,
            reasoning=reasoning,
            user_prompt=user_prompt,
            assistant_response=assistant_response,
            followup_feedback=followup_feedback,
            created_at=current_time,
        )

        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO llm_judge_evaluations (
                    id, trace_id, session_id, reward, verdict, failure_domain,
                    confidence, reasoning, user_prompt, assistant_response,
                    followup_feedback, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.id,
                    evaluation.trace_id,
                    evaluation.session_id,
                    evaluation.reward,
                    evaluation.verdict,
                    evaluation.failure_domain,
                    evaluation.confidence,
                    evaluation.reasoning,
                    evaluation.user_prompt,
                    evaluation.assistant_response,
                    evaluation.followup_feedback,
                    evaluation.created_at,
                ),
            )

        return evaluation

    def evaluate(
        self,
        *,
        trace_id: str,
        session_id: str = "",
        user_prompt: str,
        assistant_response: str,
        followup_feedback: str = "",
        events_summary: str = "",
        provider: Any | None = None,
        now: float | None = None,
    ) -> LLMJudgeEvaluation | None:
        import asyncio
        import concurrent.futures

        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        coro = self.evaluate_async(
            trace_id=trace_id,
            session_id=session_id,
            user_prompt=user_prompt,
            assistant_response=assistant_response,
            followup_feedback=followup_feedback,
            events_summary=events_summary,
            provider=provider,
            now=now,
        )

        if loop is not None and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(asyncio.run, coro).result()

        return asyncio.run(coro)

    def apply_judgment(self, evaluation: LLMJudgeEvaluation) -> None:
        """Propagate LLM judge reward and fault attribution into credit assignment and replay buffer."""
        from .credit_assignment import CreditAssignmentEngine
        from .replay_buffer import ExperienceReplayBuffer
        from .trace import TraceStore

        # 1. Update experience replay buffer reward, priority, and metadata
        try:
            replay_buf = ExperienceReplayBuffer(self.home)
            with connect(replay_buf.db_path) as conn:
                conn.execute(
                    """
                    UPDATE experience_replay_buffer
                    SET reward = ?, priority = ?,
                        metadata_json = json_set(metadata_json, '$.llm_judge', json(?))
                    WHERE trace_id = ?
                    """,
                    (
                        evaluation.reward,
                        round((abs(evaluation.reward) + 0.01) ** 0.60, 6),
                        json.dumps(evaluation.to_dict(), ensure_ascii=False),
                        evaluation.trace_id,
                    ),
                )
        except Exception as exc:
            logger.debug("LLM judge replay buffer update skipped: %s", exc)

        # 2. If negative feedback / failure, trigger credit assignment blame backpropagation
        if evaluation.reward < -0.2:
            effective_domain = evaluation.failure_domain
            if not effective_domain or effective_domain == "none":
                effective_domain = "planner_or_parser"
            try:
                credit_engine = CreditAssignmentEngine(self.home)
                trace_store = TraceStore(self.home)
                events = trace_store.list_events(evaluation.trace_id)
                credit_engine.backprop_trace(
                    trace_id=evaluation.trace_id,
                    outcome="failure",
                    failure_domain=effective_domain,
                    events=events,
                    evidence={"llm_judge": evaluation.to_dict()},
                    custom_reward=evaluation.reward,
                )
            except Exception as exc:
                logger.warning("LLM judge credit backpropagation failed: %s", exc)

    def get_evaluation(self, trace_id: str) -> LLMJudgeEvaluation | None:
        sql = """
            SELECT id, trace_id, session_id, reward, verdict, failure_domain,
                   confidence, reasoning, user_prompt, assistant_response,
                   followup_feedback, created_at
            FROM llm_judge_evaluations
            WHERE trace_id = ?
            ORDER BY created_at DESC LIMIT 1
        """
        with connect(self.db_path) as conn:
            row = conn.execute(sql, (trace_id,)).fetchone()
        if row is None:
            return None
        return LLMJudgeEvaluation(*row)

    def list_evaluations(self, limit: int = 50) -> list[LLMJudgeEvaluation]:
        sql = """
            SELECT id, trace_id, session_id, reward, verdict, failure_domain,
                   confidence, reasoning, user_prompt, assistant_response,
                   followup_feedback, created_at
            FROM llm_judge_evaluations
            ORDER BY created_at DESC LIMIT ?
        """
        with connect(self.db_path) as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [LLMJudgeEvaluation(*row) for row in rows]
