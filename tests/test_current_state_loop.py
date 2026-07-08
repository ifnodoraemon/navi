from __future__ import annotations

from navi.control import CurrentStateBuilder, SurfaceContext, current_state_facts
from navi.goals import GoalStore
from navi.loop_contracts import (
    GoalSpec,
    LoopSpec,
    TimeoutPolicy,
    VerificationKind,
    VerificationStep,
)
from navi.loop_runs import LoopRunStore
from navi.workspaces import ShadowWorkspaceManager


def _loop_spec(goal_id: str, workspace: str) -> LoopSpec:
    return LoopSpec.from_goal(
        GoalSpec(
            objective="Move Navi toward durable loop execution",
            scope=(f"repo:{workspace}",),
            constraints=("state graph decisions read CurrentState",),
            acceptance_criteria=("current state includes active loop runs",),
            permission_ceiling="write",
        ),
        goal_id=goal_id,
        allowed_capabilities=("filesystem.write", "test.run"),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.COMMAND_EXIT_CODE,
                name="tests",
                command="pytest -q",
                timeout=TimeoutPolicy(seconds=120),
            ),
        ),
    )


def test_current_state_facts_include_active_goal_and_loop_run_state(tmp_path):
    goal = GoalStore(tmp_path).create(
        objective="durable loop execution",
        workspace=str(tmp_path),
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        session_id="session-1",
        task_status="in_progress",
    )
    spec = _loop_spec(goal.id, str(tmp_path))
    loop_run = LoopRunStore(tmp_path).create_run(spec)

    state = CurrentStateBuilder(tmp_path).build(
        SurfaceContext(
            home=tmp_path,
            source="weixin",
            peer_id="peer-1",
            sender_id="sender-1",
            session_id="session-1",
            workspace=str(tmp_path),
        )
    )
    facts = current_state_facts(state)

    assert facts["active_goals"][0]["id"] == goal.id
    assert facts["goal_state"]["active_goals"][0]["objective"] == "durable loop execution"
    assert facts["goal_state"]["active_goals"][0]["task_status"] == "in_progress"
    assert facts["active_loop_runs"][0]["run_id"] == loop_run.run_id
    assert facts["loop_run_state"]["active_loop_runs"][0]["loop_spec_id"] == spec.id
    assert facts["budget_state"]["decision"] == "allow"
    assert facts["workspace_state"]["workspace"] == str(tmp_path)
    assert facts["connector_state"]["source"] == "weixin"


def test_current_state_filters_loop_runs_by_visible_goal_context(tmp_path):
    visible_goal = GoalStore(tmp_path).create(
        objective="visible goal",
        workspace=str(tmp_path),
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
    )
    hidden_goal = GoalStore(tmp_path).create(
        objective="hidden goal",
        workspace=str(tmp_path),
        source="telegram",
        peer_id="other-peer",
        sender_id="other-sender",
    )
    visible_run = LoopRunStore(tmp_path).create_run(_loop_spec(visible_goal.id, str(tmp_path)))
    LoopRunStore(tmp_path).create_run(_loop_spec(hidden_goal.id, str(tmp_path)))

    facts = current_state_facts(
        CurrentStateBuilder(tmp_path).build(
            SurfaceContext(
                home=tmp_path,
                source="weixin",
                peer_id="peer-1",
                sender_id="sender-1",
                workspace=str(tmp_path),
            )
        )
    )

    assert [item["id"] for item in facts["active_goals"]] == [visible_goal.id]
    assert [item["run_id"] for item in facts["active_loop_runs"]] == [visible_run.run_id]


def test_current_state_includes_active_shadow_workspaces(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("base\n", encoding="utf-8")
    shadow = ShadowWorkspaceManager(tmp_path).create_shadow(
        run_id="loop-run-1",
        workspace=workspace,
    )

    facts = current_state_facts(
        CurrentStateBuilder(tmp_path).build(
            SurfaceContext(
                home=tmp_path,
                source="cli",
                peer_id="cli",
                sender_id="tester",
                workspace=str(workspace),
            )
        )
    )

    assert facts["workspace_state"]["shadow_workspace"] == shadow.shadow_workspace
    assert facts["workspace_state"]["shadow_workspaces"][0]["run_id"] == "loop-run-1"
