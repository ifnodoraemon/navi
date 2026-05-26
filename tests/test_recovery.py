from __future__ import annotations

import json

from navi.recovery import RecoveryPlanner


def test_recovery_planner_recommends_task_progression():
    plan = RecoveryPlanner().plan_completion_failure(
        block_reason=(
            "completion verifier blocked final answer: task task-1 is still pending; "
            "prepare it before reporting completion."
        ),
        events=[],
    )

    assert plan.recommended == "continue"
    assert plan.choices[0].tool == "task.prepare"
    assert plan.choices[0].args == {"task_id": "task-1"}
    assert json.loads(plan.to_observation().split("\n", 1)[1])["choices"][0]["kind"] == "continue"


def test_recovery_planner_recommends_finishing_failed_cleanup():
    plan = RecoveryPlanner().plan_completion_failure(
        block_reason="completion verifier blocked final answer: task.delete left 2 failed task records.",
        events=[
            {
                "tool": "task.delete",
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
    assert plan.choices[0].tool == "task.delete"
    assert plan.choices[0].args == {"status": "failed", "source": "watch"}
    assert {choice.kind for choice in plan.choices} >= {"continue", "ask_user", "rollback_proposal"}
