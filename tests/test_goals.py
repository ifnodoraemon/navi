from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from navi.goals import GoalStore
from navi.lifecycle import Acceptance, Governance, Phase, Resolution
from navi.runs import RunStore


def test_goal_store_tracks_task_lifecycle_with_evidence(tmp_path):
    runs = RunStore(tmp_path)
    task = runs.create("durable task", phase=Phase.PENDING, workspace=str(tmp_path))
    goals = GoalStore(tmp_path)
    goal = goals.create(objective=task.prompt, run_id=task.id, workspace=task.workspace, evidence={"created_from": "test"})

    assert goal.phase == Phase.RUNNING
    assert goal.resolution == Resolution.NONE
    assert json.loads(goal.evidence_json)["created_from"] == "test"

    completed = runs.update_run(
        task.id,
        phase=Phase.ENDED,
        acceptance=Acceptance.ACCEPTED,
        resolution=Resolution.SUCCESS,
        result_summary="done",
    )
    assert completed is not None
    updated = goals.update_for_run(
        completed,
        evidence={"run_phase": completed.phase, "run_resolution": completed.resolution, "result": "ok"},
    )
    assert updated is not None
    assert updated.phase == Phase.ENDED
    assert updated.resolution == Resolution.SUCCESS
    assert updated.blocked_reason == ""
    assert updated.completed_at > 0
    evidence = json.loads(updated.evidence_json)
    assert evidence["created_from"] == "test"
    assert evidence["result"] == "ok"

    failed = runs.update_run(
        task.id,
        phase=Phase.ENDED,
        acceptance=Acceptance.REJECTED,
        resolution=Resolution.FAILED,
        error="execution failed",
    )
    assert failed is not None
    blocked = goals.update_for_run(failed)
    assert blocked is not None
    assert blocked.phase == Phase.ENDED
    assert blocked.resolution == Resolution.FAILED
    assert blocked.blocked_reason == ""
    assert json.loads(blocked.evidence_json).get("error", "") == ""

    rejected = runs.update_run(
        task.id,
        phase=Phase.ENDED,
        acceptance=Acceptance.REJECTED,
        resolution=Resolution.FAILED,
        error="rejected",
    )
    assert rejected is not None
    rejected_goal = goals.update_for_run(rejected)
    assert rejected_goal is not None
    assert rejected_goal.phase == Phase.ENDED
    assert rejected_goal.resolution == Resolution.FAILED

    assert [event.event_type for event in goals.list_events(goal.id)] == [
        "goal.created",
        "goal.run_state",
        "goal.run_state",
        "goal.run_state",
    ]


def test_goal_stop_condition_reached_on_timeout(tmp_path):
    goals = GoalStore(tmp_path)
    goal = goals.create(
        objective="long-running goal",
        workspace=str(tmp_path),
        session_id="s1",
        timeout=3600.0,
    )
    # Freshly created goal is within its timeout budget.
    assert goals.stop_condition_reached(goal.id) == ""

    # Backdate created_at past the timeout to simulate an elapsed budget.
    import sqlite3 as _sqlite3

    with closing(_sqlite3.connect(tmp_path / "goals.db")) as conn:
        with conn:
            conn.execute(
                "UPDATE goals SET created_at = ? WHERE id = ?",
                (goal.created_at - 7200.0, goal.id),
            )
    reason = goals.stop_condition_reached(goal.id)
    assert reason == "timeout_reached"
    assert goals.stop_condition_facts(goal.id)["timeout_seconds"] == 3600

    # A goal with no declared stop condition never trips the boundary.
    open_goal = goals.create(objective="open goal", workspace=str(tmp_path))
    assert goals.stop_condition_reached(open_goal.id) == ""


def test_cron_queries_preserve_goal_dataclass_field_order(tmp_path):
    goals = GoalStore(tmp_path)
    created = goals.create(
        objective="ordered recurring goal",
        workspace=str(tmp_path),
        source="weixin",
        peer_id="peer-1",
        sender_id="user-1",
        stop_condition="explicit-stop",
        timeout=321.0,
        max_retries=7,
        cron_schedule="54 11 * * *",
        next_run_at=1.0,
    )

    listed = goals.list_cron_goals()[0]
    due = goals.due_cron_goals(2.0)[0]
    found = goals.find_active_cron_goal(
        objective=created.objective,
        cron_schedule=created.cron_schedule,
        source=created.source,
        peer_id=created.peer_id,
        sender_id=created.sender_id,
    )

    for queried in (listed, due, found):
        assert queried is not None
        assert queried.stop_condition == "explicit-stop"
        assert queried.timeout == 321.0
        assert queried.max_retries == 7
        assert queried.created_at == created.created_at
        assert queried.updated_at == created.updated_at
        assert queried.completed_at == created.completed_at


def test_goal_stop_condition_reached_on_retry_ceiling(tmp_path):
    goals = GoalStore(tmp_path)
    goal = goals.create(
        objective="retried goal",
        workspace=str(tmp_path),
        max_retries=2,
    )
    assert goals.stop_condition_reached(goal.id) == ""

    # Record two failed run-state events while the goal stays active, simulating
    # two retries that did not resolve the objective.
    goals.record_event(
        goal.id,
        "goal.run_state",
        phase=Phase.RUNNING,
        governance=Governance.NONE,
        acceptance=Acceptance.NONE,
        resolution=Resolution.BLOCKED,
    )
    assert goals.stop_condition_reached(goal.id) == ""
    goals.record_event(
        goal.id,
        "goal.run_state",
        phase=Phase.RUNNING,
        governance=Governance.NONE,
        acceptance=Acceptance.NONE,
        resolution=Resolution.BLOCKED,
    )

    reason = goals.stop_condition_reached(goal.id)
    assert reason == "retry_ceiling_reached"
    assert goals.stop_condition_facts(goal.id)["retry_count"] == 2


def test_goal_store_rejects_schema_drift(tmp_path):
    with closing(sqlite3.connect(tmp_path / "goals.db")) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE goals (
                    id TEXT PRIMARY KEY,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO goals(
                    id, objective, status, source, peer_id, sender_id,
                    session_id, workspace, created_at, updated_at
                )
                VALUES ('drift-goal', 'drift objective', 'active', '', '', '', '', '', 1, 1)
                """
            )
            conn.execute(
                """
                CREATE TABLE goal_events (
                    id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )

    with pytest.raises(RuntimeError, match="goals schema mismatch"):
        GoalStore(tmp_path)


def test_goal_store_rejects_task_id_schema_drift(tmp_path):
    with closing(sqlite3.connect(tmp_path / "goals.db")) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE goals (
                    id TEXT PRIMARY KEY,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO goals(
                    id, objective, status, source, peer_id, sender_id,
                    session_id, workspace, task_id, created_at, updated_at
                )
                VALUES ('drift-goal', 'drift objective', 'active', '', '', '', '', '', 'drift-run', 1, 1)
                """
            )
            conn.execute(
                """
                CREATE TABLE goal_events (
                    id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )

    with pytest.raises(RuntimeError, match="goals schema mismatch"):
        GoalStore(tmp_path)
