from __future__ import annotations

import shlex
import sys

from navi.goals import GoalStore
from navi.lifecycle import Acceptance, Governance, Phase, Resolution
from navi.loop_contracts import LoopTerminalState, VerificationKind
from navi.loop_control_service import LoopControlService, OpenGoalRequest
from navi.loop_runs import LoopRunStore


def _command(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def test_loop_control_service_opens_goal_without_executing_state_graph(tmp_path):
    result = LoopControlService(tmp_path).open_goal(
        OpenGoalRequest(
            objective="verify slow path",
            workspace=str(tmp_path),
            source="cli",
            peer_id="cli",
            sender_id="tester",
            verification_command=_command("print('ok')"),
            timeout_seconds=5,
        )
    )

    assert result.run.phase == Phase.RUNNING
    assert result.run.acceptance == Acceptance.NONE
    assert result.run.resolution == Resolution.NONE
    assert result.loop_run.terminal_state == ""
    assert result.state_graph_result is None
    assert result.to_facts()["completion_evidence"] is False
    assert LoopRunStore(tmp_path).get_run(result.loop_run.run_id) is not None


def test_loop_control_service_creates_human_verification_loop_without_running(tmp_path):
    result = LoopControlService(tmp_path).open_goal(
        OpenGoalRequest(
            objective="needs human verification",
            workspace=str(tmp_path),
            source="cli",
            peer_id="cli",
            sender_id="tester",
            timeout_seconds=5,
        )
    )

    assert result.run.phase == Phase.RUNNING
    assert result.run.resolution == Resolution.NONE
    assert result.loop_spec.verification_ladder[0].kind == VerificationKind.HUMAN_APPROVAL
    assert result.loop_run.terminal_state == ""
    assert result.to_facts()["completion_evidence"] is False


def test_loop_control_service_resumes_persisted_loop_spec_from_checkpoint(tmp_path):
    service = LoopControlService(tmp_path)
    opened = service.open_goal(
        OpenGoalRequest(
            objective="create durable run",
            workspace=str(tmp_path),
            source="cli",
            peer_id="cli",
            sender_id="tester",
            verification_command=_command("print('ok')"),
            timeout_seconds=5,
            auto_start=False,
        )
    )

    assert opened.loop_run.terminal_state == ""
    assert GoalStore(tmp_path).get(opened.goal.id) is not None

    resumed = service.resume_loop(loop_run_id=opened.loop_run.run_id, workspace=str(tmp_path))

    assert resumed.loop_run.run_id == opened.loop_run.run_id
    assert resumed.loop_run.terminal_state == ""
    assert resumed.run.phase == Phase.RUNNING
    assert resumed.run.resolution == Resolution.NONE
    assert resumed.state_graph_result is None
    assert resumed.to_facts()["state_transition"] == "resumed"


def test_loop_control_service_resumes_goal_by_goal_id(tmp_path):
    service = LoopControlService(tmp_path)
    opened = service.open_goal(
        OpenGoalRequest(
            objective="resume by goal",
            workspace=str(tmp_path),
            source="cli",
            peer_id="cli",
            sender_id="tester",
            verification_command=_command("print('ok')"),
            timeout_seconds=5,
            auto_start=False,
        )
    )

    resumed = service.resume_goal(goal_id=opened.goal.id, workspace=str(tmp_path))

    assert resumed.goal.id == opened.goal.id
    assert resumed.loop_run.run_id == opened.loop_run.run_id
    assert resumed.loop_run.terminal_state == ""
    assert resumed.run.resolution == Resolution.NONE


def test_loop_control_service_cancels_active_goal_through_control_edge(tmp_path):
    service = LoopControlService(tmp_path)
    opened = service.open_goal(
        OpenGoalRequest(
            objective="cancel durable goal",
            workspace=str(tmp_path),
            source="cli",
            peer_id="cli",
            sender_id="tester",
            verification_command=_command("print('ok')"),
            timeout_seconds=5,
            auto_start=False,
        )
    )

    cancelled = service.cancel_goal(goal_id=opened.goal.id, reason="user changed direction")

    assert cancelled.to_facts()["state_transition"] == "cancelled"
    assert cancelled.loop_run.terminal_state == LoopTerminalState.CANCELLED
    assert cancelled.run.phase == Phase.ENDED
    assert cancelled.run.governance == Governance.NONE
    assert cancelled.run.acceptance == Acceptance.REJECTED
    assert cancelled.run.resolution == Resolution.CANCELED
    assert cancelled.goal.phase == Phase.ENDED
    assert cancelled.goal.resolution == Resolution.CANCELED


def test_loop_control_service_reads_goal_state(tmp_path):
    service = LoopControlService(tmp_path)
    opened = service.open_goal(
        OpenGoalRequest(
            objective="read durable goal state",
            workspace=str(tmp_path),
            source="cli",
            peer_id="cli",
            sender_id="tester",
            verification_command=_command("print('ok')"),
            timeout_seconds=5,
            auto_start=False,
        )
    )

    by_goal = service.goal_state(goal_id=opened.goal.id)
    by_loop = service.goal_state(loop_run_id=opened.loop_run.run_id)
    active = service.goal_state()

    assert by_goal["state_transition"] == "state_read"
    assert by_goal["goal"]["id"] == opened.goal.id
    assert by_goal["loop_runs"][0]["run_id"] == opened.loop_run.run_id
    assert by_loop["loop_run"]["goal_id"] == opened.goal.id
    assert active["active_loop_runs"][0]["run_id"] == opened.loop_run.run_id
