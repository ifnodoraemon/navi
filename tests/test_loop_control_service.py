from __future__ import annotations

import json
import shlex
import sys
from dataclasses import replace

import pytest

from navi.db import connect
from navi.delivery_outbox import DeliveryOutboxStore
from navi.goals import GoalStore
from navi.lifecycle import Acceptance, Governance, Phase, Resolution
from navi.loop_contracts import LoopNode, LoopTerminalState, VerificationKind
from navi.loop_control_service import LoopControlService, OpenGoalRequest
from navi.loop_runs import LoopRunStore
from navi.state_graph import StateGraphRunResult


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
    assert result.to_facts()["execution_mode"] == "background"
    assert result.run.kind == "loop:durable_goal"
    assert json.loads(result.goal.evidence_json)["loop_kind"] == "durable_goal"
    assert result.to_facts()["budget_policy"]["call_budget"] == 7
    assert result.loop_spec.budget_policy.token_budget == 123
    assert result.loop_spec.budget_policy.cost_budget == 4.5
    assert result.loop_spec.budget_policy.qps_limit == 3
    assert result.loop_spec.budget_policy.max_concurrent == 2
    assert result.loop_spec.goal.metadata["route"] == "unified_loop"
    assert result.loop_spec.goal.metadata["loop_kind"] == "durable_goal"
    assert result.loop_spec.goal.metadata["execution_mode"] == "background"
    assert result.loop_run.evidence["execution_mode"] == "background"
    assert result.loop_spec.goal.metadata["session_id"] == "session-unified"
    assert result.loop_spec.goal.metadata["sender_id"] == "tester"
    assert LoopRunStore(tmp_path).get_run(result.loop_run.run_id) is not None


def test_loop_control_service_does_not_invent_a_checker_acceptance_criterion(tmp_path):
    result = LoopControlService(tmp_path).open_goal(
        OpenGoalRequest(
            objective="report the current account usage",
            workspace=str(tmp_path),
            allowed_capabilities=("account.usage", "respond"),
            auto_start=False,
        )
    )

    assert result.loop_spec.goal.acceptance_criteria == ()


def test_background_converged_result_creates_delivery_outbox(tmp_path):
    service = LoopControlService(tmp_path)
    token = "OUTBOX_PRIVATE_TOKEN"
    opened = service.open_goal(
        OpenGoalRequest(
            objective="send accepted lesson",
            workspace=str(tmp_path),
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            allowed_capabilities=("respond",),
            auto_start=False,
            execution_mode="background",
        )
    )
    terminal = replace(
        opened.loop_run,
        terminal_state=LoopTerminalState.CONVERGED,
        evidence={**opened.loop_run.evidence, "execution_mode": "background"},
    )

    projected = service.apply_state_graph_result(
        opened,
        StateGraphRunResult(
            run_state=terminal,
            evidence={
                "responded_message": "Accepted lesson body.",
                "capability_result": {
                    "facts": {
                        "private_evidence": {"smoke_token": token},
                        "private_evidence_provenance": "respond.private_evidence",
                    }
                },
            },
        ),
    )

    accepted = service.goals.accepted_result_for_run(opened.run.id)
    assert projected.run.phase == Phase.PAUSED
    assert projected.run.acceptance == Acceptance.UNVERIFIED
    assert projected.run.resolution == Resolution.NONE
    assert accepted["body"] == "Accepted lesson body."
    assert token not in accepted["body"]
    assert accepted["body_provenance"] == "state_graph.evidence.responded_message"
    assert accepted["delivery_status"] == "pending"
    assert opened.loop_spec.goal.metadata["task_context"]["delivery"] == {
        "stage": "post_semantic_acceptance_outbox",
        "transport_receipt_available": False,
    }

    claimed = service.goals.claim_pending_delivery_outbox(channel="weixin")
    assert len(claimed) == 1
    assert claimed[0].trace_id == opened.run.id
    service.goals.mark_delivery_outbox_failed(claimed[0].id, error="provider unavailable")
    assert service.goals.claim_pending_delivery_outbox(channel="weixin") == []
    status = DeliveryOutboxStore(tmp_path).get(claimed[0].id)
    assert status is not None
    assert status.status == "failed"


def test_stale_sending_delivery_is_requeued_with_the_same_idempotency_key(tmp_path):
    service = LoopControlService(tmp_path)
    opened = service.open_goal(
        OpenGoalRequest(
            objective="deliver once",
            workspace=str(tmp_path),
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            auto_start=False,
            execution_mode="background",
        )
    )
    outbox = service.goals.record_result_delivery_outbox(
        run=opened.run,
        goal=opened.goal,
        body="accepted body",
        body_provenance="state_graph.evidence.responded_message",
        channel="weixin",
    )
    assert outbox is not None
    claimed = service.goals.claim_pending_delivery_outbox(channel="weixin")
    assert len(claimed) == 1

    recovered = service.goals.mark_stale_sending_delivery_outbox_unknown(
        channel="weixin",
        now=claimed[0].updated_at + 301,
    )

    assert len(recovered) == 1
    assert recovered[0].status == "pending"
    assert recovered[0].id == claimed[0].id
    assert recovered[0].attempts == 1
    reclaimed = DeliveryOutboxStore(tmp_path).claim_ready(
        channel="weixin",
        now=recovered[0].updated_at,
    )
    assert [item.id for item in reclaimed] == [claimed[0].id]
    assert reclaimed[0].attempts == 2


def test_loop_control_service_rejects_unknown_loop_kind(tmp_path):
    with pytest.raises(ValueError, match="unsupported loop_kind"):
        LoopControlService(tmp_path).open_goal(
            OpenGoalRequest(
                objective="unknown loop contract",
                workspace=str(tmp_path),
                loop_kind="unknown-kind",
            )
        )

    assert GoalStore(tmp_path).list(limit=10) == []


def test_loop_control_service_rejects_incomplete_persisted_spec(tmp_path):
    service = LoopControlService(tmp_path)
    opened = service.open_goal(
        OpenGoalRequest(
            objective="strict persisted loop contract",
            workspace=str(tmp_path),
            auto_start=False,
        )
    )
    raw = json.loads(service.loop_runs.get_spec_json(opened.loop_spec.id))
    raw["goal"].pop("permission_ceiling")
    with connect(service.loop_runs.db_path) as conn:
        conn.execute(
            "UPDATE loop_specs SET spec_json = ? WHERE id = ?",
            (json.dumps(raw), opened.loop_spec.id),
        )

    with pytest.raises(KeyError, match="permission_ceiling"):
        service.resume_goal(goal_id=opened.goal.id)


def test_active_loop_lookup_is_not_hidden_by_newer_terminal_history(tmp_path):
    service = LoopControlService(tmp_path)
    opened = service.open_goal(
        OpenGoalRequest(
            objective="retain active loop authority",
            workspace=str(tmp_path),
            auto_start=False,
        )
    )
    for _ in range(101):
        service.loop_runs.create_run(
            opened.loop_spec,
            terminal_state=LoopTerminalState.CONVERGED,
        )

    cancelled = service.cancel_goal(goal_id=opened.goal.id, reason="explicit stop")

    assert cancelled.loop_run.run_id == opened.loop_run.run_id
    assert cancelled.loop_run.terminal_state == LoopTerminalState.CANCELLED


def test_scheduled_goal_registration_converges_without_entering_active_queue(tmp_path):
    service = LoopControlService(tmp_path)
    request = OpenGoalRequest(
        objective="remind me to eat and teach one AI concept",
        workspace=str(tmp_path),
        loop_kind="scheduled",
        cron_schedule="15 8 * * *",
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
    assert registered.goal.cron_schedule == "15 8 * * *"
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
                cron_schedule="15 8",
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
            cron_schedule="15 8 * * *",
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
    assert occurrence.to_facts()["execution_mode"] == "background"
    assert occurrence.loop_run.evidence["execution_mode"] == "background"
    assert occurrence.loop_run.terminal_state == ""
    assert [item.run_id for item in LoopRunStore(tmp_path).list_active()] == [
        occurrence.loop_run.run_id
    ]
    trigger = occurrence.loop_spec.goal.metadata["trigger_facts"]
    assert trigger["type"] == "scheduled_occurrence"
    assert trigger["schedule_goal_id"] == registered.goal.id
    assert trigger["cron_schedule"] == "15 8 * * *"
    assert trigger["occurrence_number"] == 1
    assert trigger["prior_occurrences"] == []
    task_context = occurrence.loop_spec.goal.metadata["task_context"]
    assert task_context["lineage"] == {
        "id": registered.goal.id,
        "kind": "recurring_goal",
        "current_goal_id": occurrence.goal.id,
        "parent_goal_id": registered.goal.id,
    }
    assert task_context["progress"]["scope"] == "lineage"
    assert task_context["progress"]["sequence_number"] == 1
    assert task_context["progress"]["authority"] == ("same_lineage_authoritative_prior_items")
    assert task_context["progress"]["authoritative_prior_items"] == []
    assert task_context["progress"]["ambient_history_authoritative"] is False


def test_scheduled_occurrence_exposes_prior_output_and_delivery_as_facts(tmp_path):
    service = LoopControlService(tmp_path)
    registered = service.open_goal(
        OpenGoalRequest(
            objective="teach a progressive daily topic",
            workspace=str(tmp_path),
            loop_kind="scheduled",
            cron_schedule="15 8 * * *",
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            allowed_capabilities=("respond",),
        )
    )
    first = service.open_scheduled_occurrence(registered.goal)
    first_run = service.runs.update_run(
        first.run.id,
        phase=Phase.ENDED,
        governance="none",
        acceptance="accepted",
        resolution=Resolution.SUCCESS,
        result_summary="Lesson 1: foundations. Next topic: supervised learning.",
        error="",
    )
    assert first_run is not None
    service.goals.update_for_run(first_run)
    service.goals.record_result_delivery_outbox(
        run=first_run,
        goal=first.goal,
        body="Lesson 1: foundations. Next topic: supervised learning.",
        body_provenance="state_graph.evidence.responded_message",
        channel="weixin",
        trace_id=first.loop_run.run_id,
    )
    service.goals.record_delivery(
        run_id=first.run.id,
        channel="weixin",
        text_preview="Lesson 1: foundations.",
        text_length=len(first_run.result_summary),
        media_count=0,
    )

    second = service.open_scheduled_occurrence(registered.goal)

    trigger = second.loop_spec.goal.metadata["trigger_facts"]
    assert trigger["occurrence_number"] == 2
    assert len(trigger["prior_occurrences"]) == 1
    previous = trigger["prior_occurrences"][0]
    assert previous["accepted_result_text"] == first_run.result_summary
    assert previous["accepted_result"]["body_provenance"] == (
        "state_graph.evidence.responded_message"
    )
    assert previous["delivery"]["state_transition"] == "delivered"
    assert previous["delivery"]["channel"] == "weixin"
    task_context = second.loop_spec.goal.metadata["task_context"]
    assert task_context["lineage"]["id"] == registered.goal.id
    assert task_context["lineage"]["current_goal_id"] == second.goal.id
    assert task_context["progress"]["scope"] == "lineage"
    assert task_context["progress"]["sequence_number"] == 2
    assert task_context["progress"]["authoritative_prior_items"] == [previous]


def test_scheduled_goal_can_be_cancelled_after_registration(tmp_path):
    service = LoopControlService(tmp_path)
    registered = service.open_goal(
        OpenGoalRequest(
            objective="daily reminder",
            workspace=str(tmp_path),
            loop_kind="scheduled",
            cron_schedule="15 8 * * *",
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

    def fail_loop_create(spec, **kwargs):
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
    assert opened.to_facts()["execution_mode"] == "manual"
    assert opened.loop_run.evidence["execution_mode"] == "manual"

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


def test_loop_control_service_cancels_goal_waiting_for_approval(tmp_path):
    service = LoopControlService(tmp_path)
    opened = service.open_goal(
        OpenGoalRequest(
            objective="delete file after approval",
            workspace=str(tmp_path),
            source="weixin",
            peer_id="peer-1",
            sender_id="sender-1",
            auto_start=False,
        )
    )
    with connect(service.loop_runs.db_path) as conn:
        conn.execute(
            """
            UPDATE loop_runs
            SET node = ?, terminal_state = ?, evidence_json = ?
            WHERE id = ?
            """,
            (
                str(LoopNode.ESCALATE),
                str(LoopTerminalState.WAITING_APPROVAL),
                json.dumps({"action": "approval"}),
                opened.loop_run.run_id,
            ),
        )
    service.runs.update_run(
        opened.run.id,
        phase=Phase.PAUSED,
        governance=Governance.AWAITING_APPROVAL,
        acceptance=Acceptance.NONE,
        resolution=Resolution.NONE,
    )
    service.goals.update_state(
        opened.goal.id,
        phase=Phase.RUNNING,
        governance=Governance.AWAITING_APPROVAL,
        task_status="pending",
    )

    cancelled = service.cancel_goal(
        goal_id=opened.goal.id,
        reason="user cancelled pending approval",
    )

    assert cancelled.state_transition == "cancelled"
    assert cancelled.loop_run.terminal_state == LoopTerminalState.CANCELLED
    assert cancelled.run.phase == Phase.ENDED
    assert cancelled.run.governance == Governance.NONE
    assert cancelled.run.resolution == Resolution.CANCELED
    assert cancelled.goal.phase == Phase.ENDED
    assert cancelled.goal.governance == Governance.NONE
    assert cancelled.goal.resolution == Resolution.CANCELED
    assert cancelled.goal.task_status == "blocked"


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
