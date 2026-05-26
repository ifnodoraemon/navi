from __future__ import annotations

import json

from navi.goals import (
    GOAL_STATUS_ACTIVE,
    GOAL_STATUS_BLOCKED,
    GOAL_STATUS_REJECTED,
    GOAL_STATUS_VERIFIED_COMPLETE,
    GoalStore,
)
from navi.runs import RunStore


def test_goal_store_tracks_task_lifecycle_with_evidence(tmp_path):
    runs = RunStore(tmp_path)
    task = runs.create("durable task", status="pending")
    goals = GoalStore(tmp_path)
    goal = goals.create(objective=task.prompt, run_id=task.id, evidence={"created_from": "test"})

    assert goal.status == GOAL_STATUS_ACTIVE
    assert json.loads(goal.evidence_json)["created_from"] == "test"

    completed = runs.update_run(task.id, status="completed", result_summary="done")
    assert completed is not None
    updated = goals.update_for_run(completed, evidence={"run_status": completed.status, "result": "ok"})
    assert updated is not None
    assert updated.status == GOAL_STATUS_VERIFIED_COMPLETE
    assert updated.completed_at > 0
    evidence = json.loads(updated.evidence_json)
    assert evidence["created_from"] == "test"
    assert evidence["result"] == "ok"

    failed = runs.update_run(task.id, status="blocked", error="execution grant missing")
    assert failed is not None
    blocked = goals.update_for_run(failed)
    assert blocked is not None
    assert blocked.status == GOAL_STATUS_BLOCKED
    assert blocked.blocked_reason == "execution grant missing"

    rejected = runs.update_run(task.id, status="rejected")
    assert rejected is not None
    rejected_goal = goals.update_for_run(rejected)
    assert rejected_goal is not None
    assert rejected_goal.status == GOAL_STATUS_REJECTED

    assert [event.event_type for event in goals.list_events(goal.id)] == [
        "goal.created",
        "goal.run_status",
        "goal.run_status",
        "goal.run_status",
    ]
