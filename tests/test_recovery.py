from __future__ import annotations

import json

from navi.recovery import CompletionBlock, RecoveryPlanner


def test_recovery_planner_returns_task_state_facts():
    plan = RecoveryPlanner().plan_completion_failure(
        block=CompletionBlock(
            reason=(
                "loop checker blocked final answer: "
                "delegation run run-1 is still pending."
            ),
            run_id="run-1",
            run_status="pending",
        ),
        events=[],
    )

    assert plan.details == {
        "blocked_entity_type": "delegation_run",
        "run_id": "run-1",
        "run_status": "pending",
    }
    observation = json.loads(plan.to_observation().split("\n", 1)[1])
    assert observation == {
        "blocked": True,
        "blocked_entity_type": "delegation_run",
        "reason": (
            "loop checker blocked final answer: "
            "delegation run run-1 is still pending."
        ),
        "run_id": "run-1",
        "run_status": "pending",
        "trigger": "loop.check",
    }
    assert "choices" not in observation
    assert "recommended" not in observation


def test_recovery_planner_returns_cleanup_state_facts():
    plan = RecoveryPlanner().plan_completion_failure(
        block=CompletionBlock(
            reason=(
                "loop checker blocked final answer: "
                "delegate.delete left 2 failed delegation runs."
            ),
        ),
        events=[
            {
                "tool": "delegate.delete",
                "facts": {
                    "entity_type": "bulk_delete",
                    "completion_evidence": False,
                    "cleanup_complete": False,
                    "remaining_count": 2,
                    "source_filter": "watch",
                    "kind_filter": "",
                },
            }
        ],
    )

    assert plan.details == {
        "blocked_entity_type": "delegation_cleanup",
        "cleanup_complete": False,
        "remaining_count": 2,
        "source_filter": "watch",
    }
    assert "choices" not in plan.to_observation()
    assert "recommended" not in plan.to_observation()
