from __future__ import annotations

import json

from navi.recovery import CompletionBlock, RecoveryPlanner


def test_recovery_planner_recommends_task_progression():
    plan = RecoveryPlanner().plan_completion_failure(
        block=CompletionBlock(
            reason=(
                "completion verifier blocked final answer: "
                "delegation run run-1 is still pending."
            ),
            run_id="run-1",
            run_status="pending",
        ),
        events=[],
    )

    assert plan.recommended == "continue"
    assert plan.choices[0].tool == "delegate.prepare"
    assert plan.choices[0].args == {"run_id": "run-1"}
    observation = json.loads(plan.to_observation().split("\n", 1)[1])
    assert observation == {
        "blocked": True,
        "reason": (
            "completion verifier blocked final answer: "
            "delegation run run-1 is still pending."
        ),
        "trigger": "completion.verify",
    }
    assert "choices" not in observation
    assert "recommended" not in observation


def test_recovery_planner_recommends_finishing_failed_cleanup():
    plan = RecoveryPlanner().plan_completion_failure(
        block=CompletionBlock(
            reason=(
                "completion verifier blocked final answer: "
                "delegate.delete left 2 failed delegation runs."
            ),
        ),
        events=[
            {
                "tool": "delegate.delete",
                "facts": {
                    "cleanup_complete": False,
                    "remaining_count": 2,
                    "source_filter": "watch",
                    "kind_filter": "",
                },
            }
        ],
    )

    assert plan.recommended == "continue"
    assert plan.choices[0].tool == "delegate.delete"
    assert plan.choices[0].args == {"status": "failed", "source": "watch"}
    assert {choice.kind for choice in plan.choices} >= {"continue", "ask_user", "rollback_proposal"}
