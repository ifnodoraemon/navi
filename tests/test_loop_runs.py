from __future__ import annotations

import json

import pytest

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
        allowed_capabilities=("filesystem.write", "shell.run"),
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
