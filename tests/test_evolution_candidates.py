from __future__ import annotations

import sqlite3
import time
import json
from contextlib import closing

import pytest

from navi.actions.registry import get_action_handlers
from navi.capabilities_types import CapabilityContext
from navi.evolution_candidates import EvolutionCandidateScanner
from navi.trace import TraceStore


def _record_failure(store: TraceStore, trace_id: str, created_at: float) -> None:
    evaluation = store.record_evaluation(
        trace_id=trace_id,
        outcome="failure",
        failure_domain="planner_or_parser",
        evidence={"rule": "planner_contract_failure"},
    )
    with closing(sqlite3.connect(store.db_path)) as conn, conn:
        conn.execute(
            "UPDATE trace_evaluations SET created_at = ? WHERE id = ?",
            (created_at, evaluation.id),
        )


def test_trace_failure_candidates_are_clustered_without_creating_policy(tmp_path) -> None:
    store = TraceStore(tmp_path)
    for position in range(4):
        _record_failure(store, f"trace-{position}", 1_000 + position)
    store.record_evaluation(
        trace_id="success",
        outcome="success",
        failure_domain="none",
        evidence={"rule": "completed"},
    )

    candidates = EvolutionCandidateScanner(tmp_path).scan(
        window_days=7,
        min_occurrences=3,
        now=2_000,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.failure_domain == "planner_or_parser"
    assert candidate.evaluation_rule == "planner_contract_failure"
    assert candidate.occurrences == 4
    assert candidate.next_action == "model_review_required"


def test_trace_failure_candidate_counts_are_not_silently_truncated_at_ten_thousand(
    tmp_path,
) -> None:
    store = TraceStore(tmp_path)
    evidence_json = json.dumps({"rule": "large-cluster"})
    with closing(sqlite3.connect(store.db_path)) as conn, conn:
        conn.executemany(
            """
            INSERT INTO trace_evaluations(
                id, trace_id, outcome, failure_domain, evidence_json, created_at
            ) VALUES (?, ?, 'failure', 'planner_or_parser', ?, ?)
            """,
            (
                (f"evaluation-{index}", f"trace-{index}", evidence_json, 1_000 + index)
                for index in range(10_001)
            ),
        )

    candidates = EvolutionCandidateScanner(tmp_path).scan(
        window_days=90,
        min_occurrences=3,
        now=20_000,
    )

    assert candidates[0].occurrences == 10_001
    assert len(candidates[0].sample_trace_ids) == 10


@pytest.mark.asyncio
async def test_evolution_candidates_capability_is_read_only_and_typed(tmp_path) -> None:
    store = TraceStore(tmp_path)
    for position in range(3):
        _record_failure(store, f"trace-{position}", time.time())
    capability = get_action_handlers(tmp_path, tmp_path)["evolution.candidates"]

    result = await capability.invoke(
        {"window_days": 7, "min_occurrences": 3},
        permission="read",
        context=CapabilityContext(home=tmp_path, workspace=str(tmp_path)),
    )

    assert result.ok is True
    assert result.facts["count"] == 1
    assert result.facts["semantic_decision"] == "model_review_required"
    assert result.facts["automatic_proposal_created"] is False
    assert result.facts["automatic_apply_allowed"] is False
