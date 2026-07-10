from __future__ import annotations

import json
import shlex
import sys

import pytest

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
            objective="verify unified loop",
            workspace=str(tmp_path),
            source="cli",
            peer_id="cli",
            sender_id="tester",
            session_id="session-unified",
            verification_command=_command("print('ok')"),
            timeout_seconds=5,
            call_budget=7,
            token_budget=123,
            cost_budget=4.5,
            qps_limit=3,
            max_concurrent=2,
        )
    )

    assert result.run.phase == Phase.RUNNING
    assert result.run.acceptance == Acceptance.NONE
    assert result.run.resolution == Resolution.NONE
    assert result.loop_run.terminal_state == ""
    assert result.state_graph_result is None
    assert result.to_facts()["completion_evidence"] is False
    assert result.to_facts()["route"] == "unified_loop"
    assert result.to_facts()["loop_kind"] == "durable_goal"
    assert result.run.kind == "loop:durable_goal"
    assert json.loads(result.goal.evidence_json)["loop_kind"] == "durable_goal"
    assert result.to_facts()["budget_policy"]["call_budget"] == 7
    assert result.loop_spec.budget_policy.token_budget == 123
    assert result.loop_spec.budget_policy.cost_budget == 4.5
    assert result.loop_spec.budget_policy.qps_limit == 3
    assert result.loop_spec.budget_policy.max_concurrent == 2
    assert result.loop_spec.goal.metadata["route"] == "unified_loop"
    assert result.loop_spec.goal.metadata["loop_kind"] == "durable_goal"
    assert result.loop_spec.goal.metadata["session_id"] == "session-unified"
    assert result.loop_spec.goal.metadata["sender_id"] == "tester"
    assert LoopRunStore(tmp_path).get_run(result.loop_run.run_id) is not None


def test_scheduled_goal_registration_converges_without_entering_active_queue(tmp_path):
    service = LoopControlService(tmp_path)
    request = OpenGoalRequest(
        objective="remind me to eat and teach one AI concept",
        workspace=str(tmp_path),
        loop_kind="scheduled",
        cron_schedule="54 11 * * *",
        source="weixin",
        peer_id="peer-1",
        sender_id="user-1",
        session_id="session-1",
        allowed_capabilities=("respond",),
    )

    registered = service.open_goal(request)

    assert registered.state_transition == "scheduled"
    assert registered.run.phase == Phase.ENDED
    assert registered.run.acceptance == Acceptance.ACCEPTED
    assert registered.run.resolution == Resolution.SUCCESS
    assert registered.goal.phase == Phase.RUNNING
    assert registered.goal.acceptance == Acceptance.ACCEPTED
    assert registered.goal.task_status == "scheduled"
    assert registered.goal.cron_schedule == "54 11 * * *"
    assert registered.goal.next_run_at > registered.goal.created_at
    assert registered.loop_run.terminal_state == LoopTerminalState.CONVERGED
    assert registered.to_facts()["completion_evidence"] is True
    assert registered.to_facts()["registration_evidence"] is True
    assert LoopRunStore(tmp_path).list_active() == []

    duplicate = service.open_goal(request)
    assert duplicate.state_transition == "existing"
    assert duplicate.goal.id == registered.goal.id
    assert len(GoalStore(tmp_path).list_cron_goals()) == 1


def test_scheduled_goal_requires_valid_cron_expression(tmp_path):
    service = LoopControlService(tmp_path)

    with pytest.raises(ValueError, match="requires cron_schedule"):
        service.open_goal(
            OpenGoalRequest(
                objective="missing schedule",
                workspace=str(tmp_path),
                loop_kind="scheduled",
            )
        )

    with pytest.raises(ValueError, match="5 fields"):
        service.open_goal(
            OpenGoalRequest(
                objective="invalid schedule",
                workspace=str(tmp_path),
                loop_kind="scheduled",
                cron_schedule="54 11",
            )
        )

    with pytest.raises(ValueError, match="between 0 and 59"):
        service.open_goal(
            OpenGoalRequest(
                objective="out of range schedule",
                workspace=str(tmp_path),
                loop_kind="scheduled",
                cron_schedule="99 11 * * *",
            )
        )


def test_scheduled_occurrence_is_child_active_goal_without_recurring_cron(tmp_path):
    service = LoopControlService(tmp_path)
    registered = service.open_goal(
        OpenGoalRequest(
            objective="daily reminder",
            workspace=str(tmp_path),
            loop_kind="scheduled",
            cron_schedule="54 11 * * *",
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            allowed_capabilities=("respond",),
        )
    )

    occurrence = service.open_scheduled_occurrence(registered.goal)

    assert occurrence.run.kind == "loop:durable_goal"
    assert occurrence.run.phase == Phase.RUNNING
    assert occurrence.goal.parent_goal_id == registered.goal.id
    assert occurrence.goal.cron_schedule == ""
    assert occurrence.loop_run.terminal_state == ""
    assert [item.run_id for item in LoopRunStore(tmp_path).list_active()] == [
        occurrence.loop_run.run_id
    ]


def test_scheduled_goal_can_be_cancelled_after_registration(tmp_path):
    service = LoopControlService(tmp_path)
    registered = service.open_goal(
        OpenGoalRequest(
            objective="daily reminder",
            workspace=str(tmp_path),
            loop_kind="scheduled",
            cron_schedule="54 11 * * *",
        )
    )

    cancelled = service.cancel_goal(
        goal_id=registered.goal.id,
        reason="user no longer wants the reminder",
    )

    assert cancelled.state_transition == "cancelled"
    assert cancelled.run.resolution == Resolution.CANCELED
    assert cancelled.goal.phase == Phase.ENDED
    assert cancelled.goal.task_status == "blocked"
    assert cancelled.to_facts()["completion_evidence"] is False
    assert GoalStore(tmp_path).due_cron_goals(float("inf")) == []


def test_open_goal_compensates_cross_store_failure(tmp_path, monkeypatch):
    service = LoopControlService(tmp_path)

    def fail_loop_create(spec):
        raise RuntimeError("injected loop store failure")

    monkeypatch.setattr(service.loop_runs, "create_run", fail_loop_create)

    with pytest.raises(RuntimeError, match="injected loop store failure"):
        service.open_goal(
            OpenGoalRequest(
                objective="prove cross-store compensation",
                workspace=str(tmp_path),
            )
        )

    runs = service.runs.list(limit=10)
    assert len(runs) == 1
    assert runs[0].phase == Phase.ENDED
    assert runs[0].resolution == Resolution.FAILED
    assert runs[0].error == "loop_open_failed:RuntimeError"
    goal = service.goals.get_by_run(runs[0].id)
    assert goal is not None
    assert goal.phase == Phase.ENDED
    assert goal.resolution == Resolution.FAILED


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
    assert result.loop_spec.verification_ladder[0].kind == VerificationKind.LLM_CHECKER
    assert result.loop_spec.verification_ladder[0].required is True
    assert result.loop_spec.verification_ladder[0].evidence_key == "semantic_checker_result"
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


def test_goal_task_status_tracks_terminal_run_state(tmp_path):
    service = LoopControlService(tmp_path)
    opened = service.open_goal(
        OpenGoalRequest(
            objective="finish task status",
            workspace=str(tmp_path),
            auto_start=False,
        )
    )
    completed_run = service.runs.update_run(
        opened.run.id,
        phase=Phase.ENDED,
        governance=Governance.NONE,
        acceptance=Acceptance.ACCEPTED,
        resolution=Resolution.SUCCESS,
    )
    assert completed_run is not None

    completed_goal = service.goals.update_for_run(completed_run)

    assert completed_goal is not None
    assert completed_goal.task_status == "done"
