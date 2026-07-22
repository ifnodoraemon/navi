from __future__ import annotations

import time

import pytest

from navi.loop_contracts import (
    BudgetPolicy,
    CheckpointPolicy,
    CurrentStateSnapshot,
    GoalSpec,
    LockMode,
    LoopNode,
    LoopRunState,
    LoopSpec,
    LoopTerminalState,
    MergePlan,
    MergeResult,
    MergeStatus,
    TimeoutEvidence,
    TimeoutPolicy,
    VaultHandle,
    VerificationKind,
    VerificationStep,
    WorkspaceLock,
    WorkspaceMode,
    default_state_graph,
)


def _goal_spec() -> GoalSpec:
    return GoalSpec(
        objective="Refactor the API without losing existing behavior",
        scope=("repo:/tmp/project",),
        constraints=("do not overwrite human edits",),
        acceptance_criteria=("tests pass", "checker accepts diff evidence"),
        permission_ceiling="write",
    )


def _verification() -> tuple[VerificationStep, ...]:
    return (
        VerificationStep(
            kind=VerificationKind.COMMAND_EXIT_CODE,
            name="unit-tests",
            command="pytest -q",
            timeout=TimeoutPolicy(seconds=120),
        ),
    )


def test_loop_spec_from_goal_encodes_navi_2_contract():
    spec = LoopSpec.from_goal(
        _goal_spec(),
        goal_id="goal-1",
        allowed_capabilities=("filesystem.write", "shell.run"),
        verification_ladder=_verification(),
    )

    graph_edges = {(edge.source, edge.target, edge.condition) for edge in spec.state_graph}
    assert (LoopNode.PLAN, LoopNode.EXECUTE, "plan_ready") in graph_edges
    assert (LoopNode.EXECUTE, LoopNode.EVALUATE, "side_effect_recorded") in graph_edges
    assert (LoopNode.EVALUATE, LoopTerminalState.CONVERGED, "checker_passed") in graph_edges
    assert (LoopNode.EVALUATE, LoopNode.ESCALATE, "side_effect_commit_required") in graph_edges
    assert (LoopNode.EXECUTE, LoopTerminalState.TIMED_OUT, "hard_timeout") in graph_edges
    assert (LoopNode.EVALUATE, LoopTerminalState.CONFLICTED, "merge_conflict") in graph_edges
    for node in LoopNode:
        assert (node, LoopTerminalState.CANCELLED, "cancel_requested") in graph_edges
        assert (node, LoopTerminalState.SUPERSEDED, "superseded") in graph_edges

    assert set(spec.terminal_states) == set(LoopTerminalState)
    assert spec.workspace_policy.mode == WorkspaceMode.SHADOW
    assert spec.workspace_policy.require_three_way_merge is True
    assert spec.workspace_policy.require_locks is True
    assert spec.checkpoint_policy.before_side_effect is True
    assert spec.verification_ladder[0].timeout.seconds == 120
    assert spec.budget_policy.max_concurrent == 1
    assert spec.to_dict()["budget_policy"]["call_budget"] == 0


def test_goal_spec_uses_objective_when_no_extra_acceptance_criteria_are_declared():
    goal = GoalSpec(
        objective="report the current account usage",
        scope=("repo:/tmp/project",),
    )

    goal.validate()

    assert goal.acceptance_criteria == ()


def test_loop_spec_rejects_unsafe_execution_contracts():
    with pytest.raises(ValueError, match="side effects require a checkpoint"):
        LoopSpec(
            id="loop-1",
            goal_id="goal-1",
            goal=_goal_spec(),
            state_graph=default_state_graph(),
            allowed_capabilities=("filesystem.write",),
            verification_ladder=_verification(),
            checkpoint_policy=CheckpointPolicy(before_side_effect=False),
        ).validate()


    with pytest.raises(ValueError, match="call_budget"):
        LoopSpec(
            id="loop-1",
            goal_id="goal-1",
            goal=_goal_spec(),
            state_graph=default_state_graph(),
            allowed_capabilities=("filesystem.write",),
            verification_ladder=_verification(),
            budget_policy=BudgetPolicy(call_budget=-1),
        ).validate()


    with pytest.raises(ValueError, match="verification requires a command"):
        VerificationStep(
            kind=VerificationKind.COMMAND_EXIT_CODE,
            name="missing-command",
            command="",
        ).validate()


def test_loop_run_state_requires_checkpoint_and_terminal_states_are_final():
    state = LoopRunState(run_id="run-1", goal_id="goal-1", loop_spec_id="loop-1")

    with pytest.raises(ValueError, match="checkpoint_id"):
        state.transition(node=LoopNode.EXECUTE, checkpoint_id="")

    executed = state.transition(
        node=LoopNode.EXECUTE,
        checkpoint_id="ckpt-1",
        evidence={"planned_side_effect": "filesystem.write"},
    )
    assert executed.node == LoopNode.EXECUTE
    assert executed.checkpoint_id == "ckpt-1"
    assert executed.evidence["planned_side_effect"] == "filesystem.write"

    done = executed.transition(
        node=LoopNode.EVALUATE,
        checkpoint_id="ckpt-2",
        terminal_state=LoopTerminalState.CONVERGED,
    )
    assert done.is_terminal() is True

    with pytest.raises(ValueError, match="terminal LoopRunState"):
        done.transition(node=LoopNode.PLAN, checkpoint_id="ckpt-3")


def test_workspace_locks_detect_parallel_write_conflicts():
    now = time.time()
    writer = WorkspaceLock(
        owner_run_id="run-a",
        resource="src/navi/api.py",
        mode=LockMode.WRITE,
        lease_expiry=now + 60,
    )
    reader = WorkspaceLock(
        owner_run_id="run-b",
        resource="src/navi/api.py",
        mode=LockMode.READ,
        lease_expiry=now + 60,
    )
    other_reader = WorkspaceLock(
        owner_run_id="run-c",
        resource="src/navi/api.py",
        mode=LockMode.READ,
        lease_expiry=now + 60,
    )
    expired_writer = WorkspaceLock(
        owner_run_id="run-d",
        resource="src/navi/api.py",
        mode=LockMode.WRITE,
        lease_expiry=now - 1,
    )

    assert writer.conflicts_with(reader, now=now) is True
    assert reader.conflicts_with(other_reader, now=now) is False
    assert writer.conflicts_with(expired_writer, now=now) is False


def test_vault_handles_are_prompt_safe_and_current_state_uses_handles_only():
    handle = VaultHandle(
        uri="secret://github/default-token",
        purpose="github api",
        env_var="GITHUB_TOKEN",
    )

    prompt_data = handle.to_prompt_dict()
    assert prompt_data == {
        "handle": "secret://github/default-token",
        "purpose": "github api",
        "env_var": "GITHUB_TOKEN",
    }
    assert "token-value" not in repr(prompt_data)

    state = CurrentStateSnapshot(
        goal_state={"id": "goal-1", "objective": "deploy"},
        vault_handles=(handle,),
    )
    facts = state.control_facts()
    assert facts["vault_handle_state"] == [prompt_data]


def test_merge_and_timeout_facts_drive_terminal_or_reflect_paths():
    plan = MergePlan(
        baseline_revision="base",
        shadow_revision="agent",
        current_revision="human-edit",
        changed_paths=("src/navi/api.py",),
    )
    assert plan.real_workspace_changed() is True

    conflict = MergeResult(
        status=MergeStatus.CONFLICTED,
        conflicts=("src/navi/api.py",),
        artifact_path=".navi/conflicts/run-1",
    )
    assert conflict.terminal_state() == LoopTerminalState.CONFLICTED

    timeout = TimeoutEvidence(
        command="pytest -q",
        duration_seconds=121,
        timeout_seconds=120,
        stderr_tail="hung test",
    )
    assert timeout.to_checker_fact() == {
        "error_type": "TimeoutError",
        "command": "pytest -q",
        "duration_seconds": 121,
        "timeout_seconds": 120,
        "stdout_tail": "",
        "stderr_tail": "hung test",
        "exit_status": "timed_out",
    }
