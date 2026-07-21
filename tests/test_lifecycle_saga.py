from __future__ import annotations

import pytest

from navi.goals import GoalStore
from navi.lifecycle import Acceptance, Governance, Phase, Resolution
from navi.lifecycle_saga import LifecycleSagaStore
from navi.loop_control_service import LoopControlService, OpenGoalRequest
from navi.loop_runs import LoopRunStore
from navi.runs import RunStore


def test_lifecycle_saga_recovers_partial_cross_store_projection(tmp_path, monkeypatch):
    service = LoopControlService(tmp_path)
    opened = service.open_goal(
        OpenGoalRequest(
            objective="recover lifecycle projection",
            workspace=str(tmp_path),
            auto_start=False,
        )
    )
    store = LifecycleSagaStore(tmp_path)
    saga = store.prepare(
        operation_key="test-projection",
        run_id=opened.run.id,
        goal_id=opened.goal.id,
        run_updates={
            "phase": Phase.ENDED,
            "governance": Governance.NONE,
            "acceptance": Acceptance.ACCEPTED,
            "resolution": Resolution.SUCCESS,
            "result_summary": "done",
            "error": "",
        },
        goal_evidence={"completion_evidence": True},
    )
    original = GoalStore.update_for_run

    def fail_once(self, run, *, evidence=None):
        raise RuntimeError("simulated goal store outage")

    monkeypatch.setattr(GoalStore, "update_for_run", fail_once)
    with pytest.raises(RuntimeError, match="simulated goal store outage"):
        store.apply(saga)
    assert store.get(saga.id).status == "failed"

    monkeypatch.setattr(GoalStore, "update_for_run", original)
    assert store.recover_pending() == [saga.id]
    recovered = store.get(saga.id)
    assert recovered is not None
    assert recovered.status == "completed"
    assert service.runs.get(opened.run.id).resolution == Resolution.SUCCESS
    assert service.goals.get(opened.goal.id).task_status == "done"


def test_open_orphan_recovery_fails_stale_partial_entities(tmp_path):
    service = LoopControlService(tmp_path)
    missing_goal = service.open_goal(
        OpenGoalRequest(objective="missing goal", workspace=str(tmp_path))
    )
    missing_loop = service.open_goal(
        OpenGoalRequest(objective="missing loop", workspace=str(tmp_path))
    )
    from navi.db import connect
    from navi.paths import db_paths

    paths = db_paths(tmp_path)
    with connect(paths.goals) as conn:
        conn.execute("DELETE FROM goals WHERE id = ?", (missing_goal.goal.id,))
        conn.execute(
            "UPDATE goals SET created_at = 1, updated_at = 1 WHERE id = ?",
            (missing_loop.goal.id,),
        )
    with connect(paths.runs) as conn:
        conn.execute(
            "UPDATE runs SET created_at = 1, updated_at = 1 WHERE id IN (?, ?)",
            (missing_goal.run.id, missing_loop.run.id),
        )
    with connect(paths.loop_runs) as conn:
        conn.execute(
            "UPDATE loop_runs SET created_at = 1, updated_at = 1 WHERE id = ?",
            (missing_goal.loop_run.run_id,),
        )
        conn.execute(
            "DELETE FROM loop_runs WHERE id = ?",
            (missing_loop.loop_run.run_id,),
        )

    recovered = LifecycleSagaStore(tmp_path).recover_open_orphans(
        now=100,
        grace_seconds=10,
    )

    assert recovered["runs"] == [missing_goal.run.id]
    assert recovered["goals"] == [missing_loop.goal.id]
    assert recovered["loop_runs"] == [missing_goal.loop_run.run_id]
    assert RunStore(tmp_path).get(missing_goal.run.id).resolution == Resolution.FAILED
    assert GoalStore(tmp_path).get(missing_loop.goal.id).resolution == Resolution.FAILED
    assert (
        LoopRunStore(tmp_path).get_run(missing_goal.loop_run.run_id).terminal_state
        == "failed"
    )
