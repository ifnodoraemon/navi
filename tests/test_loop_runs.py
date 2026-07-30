from __future__ import annotations

import json

import pytest

from navi.db import connect
from navi.loop_contracts import (
    GoalSpec,
    LoopNode,
    LoopSpec,
    LoopTerminalState,
    TimeoutPolicy,
    VerificationKind,
    VerificationStep,
)
from navi.loop_runs import LoopRunStore


def _spec() -> LoopSpec:
    goal = GoalSpec(
        objective="Implement durable state graph checkpointing",
        scope=("repo:/tmp/project",),
        constraints=("persist before side effects",),
        acceptance_criteria=("checkpoint exists before transition",),
        permission_ceiling="write",
    )
    return LoopSpec.from_goal(
        goal,
        goal_id="goal-1",
        allowed_capabilities=("file.write", "shell.run"),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.COMMAND_EXIT_CODE,
                name="tests",
                command="pytest -q",
                timeout=TimeoutPolicy(seconds=120),
            ),
        ),
    )


def test_loop_run_store_persists_spec_run_checkpoint_and_transition(tmp_path):
    store = LoopRunStore(tmp_path)
    spec = _spec()

    run = store.create_run(spec)
    assert run.node == LoopNode.PLAN
    assert store.get_run(run.run_id) == run

    saved_spec = json.loads(store.get_spec_json(spec.id))
    assert saved_spec["goal_id"] == "goal-1"
    assert saved_spec["checkpoint_policy"]["before_side_effect"] is True

    with pytest.raises(ValueError, match="persisted checkpoint"):
        store.transition(
            run.run_id,
            node=LoopNode.EXECUTE,
            checkpoint_id="missing-checkpoint",
        )

    checkpoint = store.write_checkpoint(
        run.run_id,
        node=LoopNode.PLAN,
        inputs={"planner": "next action"},
        state=run.to_dict(),
    )
    transitioned = store.transition(
        run.run_id,
        node=LoopNode.EXECUTE,
        checkpoint_id=checkpoint.id,
        evidence={"side_effect": "prepared"},
    )
    assert transitioned.node == LoopNode.EXECUTE
    assert transitioned.checkpoint_id == checkpoint.id
    assert transitioned.evidence["side_effect"] == "prepared"

    checkpoints = store.list_checkpoints(run.run_id)
    assert [item.id for item in checkpoints] == [checkpoint.id]

    events = store.list_events(run.run_id)
    assert [event.event_type for event in events] == [
        "loop.run_created",
        "loop.checkpoint",
        "loop.transition",
    ]
    assert store.list_by_goal("goal-1") == [transitioned]


def test_loop_run_store_filters_active_runs_by_execution_mode(tmp_path):
    store = LoopRunStore(tmp_path)
    background = store.create_run(_spec(), evidence={"execution_mode": "background"})
    manual = store.create_run(_spec(), evidence={"execution_mode": "manual"})
    store.create_run(_spec())

    assert store.list_active_for_execution_mode("background") == [background]
    assert store.list_active_for_execution_mode("manual") == [manual]


def test_loop_run_execution_claim_is_atomic_and_recoverable_after_expiry(tmp_path):
    store = LoopRunStore(tmp_path)
    run = store.create_run(_spec(), evidence={"execution_mode": "background"})

    claimed = store.claim_for_execution(run.run_id, owner="worker-a", now=100.0)
    assert claimed is not None
    assert claimed.lease_owner == "worker-a"
    assert store.claim_for_execution(run.run_id, owner="worker-b", now=101.0) is None

    recovered = store.claim_for_execution(run.run_id, owner="worker-b", now=400.0)
    assert recovered is not None
    assert recovered.lease_owner == "worker-b"
    assert recovered.version > claimed.version


def test_execution_mode_claim_can_require_a_stale_unowned_loop(tmp_path):
    store = LoopRunStore(tmp_path)
    fresh = store.create_run(_spec(), evidence={"execution_mode": "foreground"})
    stale = store.create_run(_spec(), evidence={"execution_mode": "foreground"})
    with connect(store.db_path) as conn:
        conn.execute("UPDATE loop_runs SET updated_at = 10 WHERE id = ?", (stale.run_id,))

    claimed = store.claim_active_for_execution_mode(
        "foreground",
        owner="recovery-worker",
        now=100.0,
        updated_before=20.0,
    )

    assert [state.run_id for state in claimed] == [stale.run_id]
    assert store.get_run(fresh.run_id).lease_owner == ""


def test_execution_leases_can_be_released_by_observed_owner(tmp_path):
    store = LoopRunStore(tmp_path)
    run = store.create_run(_spec(), evidence={"execution_mode": "background"})
    claimed = store.claim_for_execution(
        run.run_id,
        owner="daemon:999999:old-process",
        lease_seconds=10_000,
        now=100.0,
    )
    assert claimed is not None
    assert store.active_execution_lease_owners() == ["daemon:999999:old-process"]

    released = store.release_execution_leases_for_owners(
        {"daemon:999999:old-process"},
        reason="execution_owner_unavailable",
        now=101.0,
    )

    assert released == [run.run_id]
    recovered = store.get_run(run.run_id)
    assert recovered is not None
    assert recovered.lease_owner == ""
    assert recovered.lease_expires_at == 0.0
    event = store.list_events(run.run_id)[-1]
    assert event.event_type == "loop.execution_lease_released"
    assert json.loads(event.evidence_json)["reason"] == "execution_owner_unavailable"


def test_expired_execution_lease_is_released_without_ending_the_loop(tmp_path):
    store = LoopRunStore(tmp_path)
    run = store.create_run(_spec())
    claimed = store.claim_for_execution(
        run.run_id,
        owner="worker-a",
        lease_seconds=10,
        now=100.0,
    )
    assert claimed is not None

    assert store.release_expired_execution_leases(now=111.0) == [run.run_id]
    recovered = store.get_run(run.run_id)
    assert recovered is not None
    assert recovered.terminal_state == ""
    assert recovered.lease_owner == ""
    assert recovered.lease_expires_at == 0.0
    assert store.list_events(run.run_id)[-1].event_type == "loop.execution_lease_released"


def test_expired_worker_cannot_fail_a_loop_after_lease_recovery(tmp_path):
    store = LoopRunStore(tmp_path)
    run = store.create_run(_spec())
    first = store.claim_for_execution(
        run.run_id,
        owner="worker-a",
        lease_seconds=10,
        now=100.0,
    )
    recovered = store.claim_for_execution(run.run_id, owner="worker-b", now=111.0)

    assert first is not None
    assert recovered is not None
    with pytest.raises(RuntimeError, match="lease is not owned"):
        store.fail_active_run(
            run.run_id,
            lease_owner="worker-a",
            now=112.0,
        )
    assert store.get_run(run.run_id).lease_owner == "worker-b"


def test_loop_run_transition_rejects_non_owner_and_preserves_active_lease(tmp_path):
    store = LoopRunStore(tmp_path)
    run = store.create_run(_spec())
    claimed = store.claim_for_execution(run.run_id, owner="worker-a")
    assert claimed is not None
    checkpoint = store.write_checkpoint(
        run.run_id,
        node=LoopNode.PLAN,
        inputs={"planner": "execute"},
        state=claimed.to_dict(),
    )

    with pytest.raises(RuntimeError, match="lease is not owned"):
        store.transition(
            run.run_id,
            node=LoopNode.EXECUTE,
            checkpoint_id=checkpoint.id,
            condition="plan_ready",
            lease_owner="worker-b",
        )

    executing = store.transition(
        run.run_id,
        node=LoopNode.EXECUTE,
        checkpoint_id=checkpoint.id,
        condition="plan_ready",
        lease_owner="worker-a",
    )
    assert executing.lease_owner == "worker-a"


def test_loop_run_store_terminal_runs_are_not_active_and_do_not_transition(tmp_path):
    store = LoopRunStore(tmp_path)
    run = store.create_run(_spec())

    with pytest.raises(ValueError, match="not allowed by LoopSpec"):
        bad_checkpoint = store.write_checkpoint(
            run.run_id,
            node=LoopNode.EVALUATE,
            inputs={},
            state=run.to_dict(),
        )
        store.transition(
            run.run_id,
            node=LoopNode.EVALUATE,
            checkpoint_id=bad_checkpoint.id,
        )

    execute_checkpoint = store.write_checkpoint(
        run.run_id,
        node=LoopNode.PLAN,
        inputs={"planner": "execute"},
        state=run.to_dict(),
    )
    executing = store.transition(
        run.run_id,
        node=LoopNode.EXECUTE,
        checkpoint_id=execute_checkpoint.id,
        condition="plan_ready",
    )
    evaluate_checkpoint = store.write_checkpoint(
        run.run_id,
        node=LoopNode.EXECUTE,
        inputs={"executor": "done"},
        state=executing.to_dict(),
    )
    evaluating = store.transition(
        run.run_id,
        node=LoopNode.EVALUATE,
        checkpoint_id=evaluate_checkpoint.id,
        condition="side_effect_recorded",
    )
    terminal_checkpoint = store.write_checkpoint(
        run.run_id,
        node=LoopNode.EVALUATE,
        inputs={"checker": "passed"},
        state=evaluating.to_dict(),
    )

    terminal = store.transition(
        run.run_id,
        node=LoopNode.EVALUATE,
        checkpoint_id=terminal_checkpoint.id,
        terminal_state=LoopTerminalState.CONVERGED,
        condition="checker_passed",
        evidence={"checker": "passed"},
    )
    assert terminal.terminal_state == LoopTerminalState.CONVERGED
    assert store.list_active() == []

    next_checkpoint = store.write_checkpoint(
        run.run_id,
        node=LoopNode.PLAN,
        inputs={},
        state=terminal.to_dict(),
    )
    with pytest.raises(ValueError, match="terminal LoopRunState"):
        store.transition(
            run.run_id,
            node=LoopNode.PLAN,
            checkpoint_id=next_checkpoint.id,
        )
