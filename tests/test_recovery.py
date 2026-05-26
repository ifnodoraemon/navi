from __future__ import annotations

import json

from navi.recovery import RecoveryPlanner


def test_recovery_planner_recommends_task_progression():
    plan = RecoveryPlanner().plan_completion_failure(
        block_reason=(
            "completion verifier blocked final answer: delegation run run-1 is still pending; "
            "prepare it before reporting completion."
        ),
        events=[],
    )

    assert plan.recommended == "continue"
    assert plan.choices[0].tool == "delegate.prepare"
    assert plan.choices[0].args == {"run_id": "run-1"}
    assert json.loads(plan.to_observation().split("\n", 1)[1])["choices"][0]["kind"] == "continue"


def test_recovery_planner_recommends_finishing_failed_cleanup():
    plan = RecoveryPlanner().plan_completion_failure(
        block_reason="completion verifier blocked final answer: delegate.delete left 2 failed delegation runs.",
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
