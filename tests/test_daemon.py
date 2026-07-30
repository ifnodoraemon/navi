from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from navi.daemon import SystemDaemon
from navi.db import connect
from navi.daemon_types import ProjectEventContext, ProactiveEvent
from navi.detectors import GitMutationDetector, PortEventDetector, ServiceLogDetector
from navi.graph import GraphStore
from navi.goals import GoalStore
from navi.loop_control_service import LoopControlService, OpenGoalRequest
from navi.loop_runs import LoopRunStore
from navi.resource_gateway import (
    GlobalResourceGateway,
    ResourceLimits,
    ResourceRequest,
    SQLiteResourceLedger,
)
from navi.memory.store import MemoryStore
from navi.trace import TraceStore


class _TransportFailingProvider:
    last_usage = None

    def __init__(self) -> None:
        self.calls = 0

    async def complete_for(self, role: str, messages, **kwargs) -> str:
        del role, messages, kwargs
        self.calls += 1
        raise httpx.ReadError("upstream connection reset")


class _TransportRecoveringProvider:
    last_usage = None

    def __init__(self) -> None:
        self.planner_calls = 0

    async def complete_for(self, role: str, messages, **kwargs) -> str:
        del messages, kwargs
        if role == "planner":
            self.planner_calls += 1
            if self.planner_calls == 1:
                raise httpx.ReadError("upstream connection reset")
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "respond",
                            "permission": "read",
                            "args": {"message": "recovered after transport retry"},
                        }
                    ]
                }
            )
        if role == "checker":
            return json.dumps(
                {
                    "passed": True,
                    "evidence_summary": "the current response completes the objective",
                }
            )
        raise AssertionError(f"unexpected provider role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "checker"]

    def usage_for(self, role: str) -> dict:
        del role
        return {}


class _CheckerTransportRecoveringProvider:
    last_usage = None

    def __init__(self) -> None:
        self.planner_calls = 0
        self.checker_calls = 0

    async def complete_for(self, role: str, messages, **kwargs) -> str:
        del messages, kwargs
        if role == "planner":
            self.planner_calls += 1
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "respond",
                            "permission": "read",
                            "args": {"message": "checker retry kept this candidate"},
                        }
                    ]
                }
            )
        if role == "checker":
            self.checker_calls += 1
            if self.checker_calls == 1:
                raise httpx.ReadError("checker connection reset")
            return json.dumps(
                {
                    "passed": True,
                    "evidence_summary": "the preserved candidate completes the objective",
                }
            )
        raise AssertionError(f"unexpected provider role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "checker"]

    def usage_for(self, role: str) -> dict:
        del role
        return {}


def test_read_log_diff_redacts_secrets_without_classifying_lines(tmp_path: Path) -> None:
    """Principle 13/16: external log content is untrusted and may contain secrets.
    The prompt-bound append facts must be redacted before they reach the model,
    while semantic error classification remains model-owned."""
    log = tmp_path / "service.log"
    body = (
        "info: starting up with api_key=sk-supersecretvalue123\n"
        "FATAL: auth failed using Bearer abcDEF123tokenvalue\n"
    )
    log.write_text(body, encoding="utf-8")

    diff, offset = ServiceLogDetector._read_log_diff(
        log, last_size=0, read_end=len(body.encode("utf-8"))
    )

    # No raw secret survives in the observed append body.
    assert "sk-supersecretvalue123" not in diff
    assert "abcDEF123tokenvalue" not in diff
    assert "[REDACTED]" in diff
    assert "FATAL" in diff
    assert offset == len(body.encode("utf-8"))


def test_read_log_diff_without_secrets_is_unchanged(tmp_path: Path) -> None:
    log = tmp_path / "clean.log"
    body = "info: request handled in 12ms\ninfo: cache warm\n"
    log.write_text(body, encoding="utf-8")

    diff, _ = ServiceLogDetector._read_log_diff(
        log, last_size=0, read_end=len(body.encode("utf-8"))
    )

    assert diff == body


@pytest.mark.asyncio
async def test_proactive_detectors_require_explicit_project_watchers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run_inline(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    # The detector contract is asynchronous; keep this unit test deterministic
    # and independent from the interpreter's default executor lifecycle.
    monkeypatch.setattr(asyncio, "to_thread", run_inline)
    (tmp_path / ".git").mkdir()
    (tmp_path / "service.log").write_text("FATAL: explicit watcher test\n", encoding="utf-8")
    context = ProjectEventContext(
        project_path=str(tmp_path),
        project_data={},
        has_active_task=False,
    )

    git_events, git_updates = await GitMutationDetector().detect(context)
    log_events, log_updates = await ServiceLogDetector().detect(context)
    port_events, port_updates = await PortEventDetector().detect(context)

    assert git_events == [] and git_updates == {}
    assert log_events == [] and log_updates == {}
    assert port_events == [] and port_updates == {}

    watched_context = ProjectEventContext(
        project_path=str(tmp_path),
        project_data={"watchers": {"logs": True}},
        has_active_task=False,
    )
    watched_events, _ = await ServiceLogDetector().detect(watched_context)
    assert [event.facts["kind"] for event in watched_events] == ["log_entries_appended"]
    assert watched_events[0].facts["evidence_contract"]["does_not_establish"] == [
        "error_classification",
        "root_cause",
        "service_health",
        "task_completion",
    ]


@pytest.mark.asyncio
async def test_active_task_defers_event_without_consuming_detector_state(tmp_path: Path) -> None:
    daemon = SystemDaemon(tmp_path, project_dir=tmp_path)
    event = ProactiveEvent(
        facts={"kind": "git_status_changed", "changed_files": ["M app.py"]},
        state_updates={"last_git_status_hash": "new-hash"},
    )
    project_data = {"last_git_status_hash": "old-hash"}

    created, changed, deferred_data = await daemon._apply_event_policy(
        event,
        project_data,
        has_active_task=True,
        workspace=str(tmp_path),
    )

    assert created is None
    assert changed is False
    assert deferred_data == project_data

    created, changed, surfaced_data = await daemon._apply_event_policy(
        event,
        deferred_data,
        has_active_task=False,
        workspace=str(tmp_path),
    )
    assert created is not None
    assert created["facts"] == event.facts
    assert "message" not in created
    assert "observation" not in created
    assert changed is True
    assert surfaced_data["last_git_status_hash"] == "new-hash"


@pytest.mark.asyncio
async def test_port_detector_returns_scoped_connectivity_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refused(*args, **kwargs):
        del args, kwargs
        raise ConnectionRefusedError

    monkeypatch.setattr(asyncio, "open_connection", refused)
    context = ProjectEventContext(
        project_path=str(tmp_path),
        project_data={
            "watchers": {"ports": [4321]},
            "port_active_4321": True,
        },
        has_active_task=False,
    )

    events, updates = await PortEventDetector().detect(context)

    assert updates == {}
    assert len(events) == 1
    assert events[0].facts["kind"] == "port_reachability_changed"
    assert events[0].facts["active"] is False
    assert events[0].facts["evidence_contract"]["does_not_establish"] == [
        "service_health",
        "service_identity",
        "task_activity",
        "task_completion",
    ]


def test_active_workspace_detection_is_not_truncated_by_global_run_limit(tmp_path: Path) -> None:
    daemon = SystemDaemon(tmp_path, project_dir=tmp_path)
    for index in range(61):
        workspace = tmp_path / f"noise-{index}"
        workspace.mkdir()
        daemon.runs.create(
            f"active noise {index}",
            workspace=str(workspace),
            phase="running",
        )
    target = tmp_path / "target-workspace"
    target.mkdir()
    daemon.runs.create("target active run", workspace=str(target), phase="running")

    assert str(target.resolve()) in daemon._active_workspaces()


def test_daemon_mutation_trace_is_evaluated(tmp_path: Path) -> None:
    daemon = SystemDaemon(tmp_path, project_dir=tmp_path)

    trace_id = daemon._record_project_graph_mutation(
        str(tmp_path),
        {"path": str(tmp_path), "last_git_status_hash": "abc"},
    )

    evaluations = TraceStore(tmp_path).list_evaluations(trace_id)
    assert len(evaluations) == 1
    assert evaluations[0].outcome == "success"
    assert evaluations[0].failure_domain == "none"


@pytest.mark.asyncio
async def test_daemon_memory_maintenance_syncs_semantic_graph(tmp_path: Path) -> None:
    daemon = SystemDaemon(tmp_path, project_dir=tmp_path)
    item = MemoryStore(tmp_path).add_item(
        "fact",
        "semantic graph maintenance fact",
        source="test",
        status="active",
        confidence=0.8,
        reason="unit test",
        provenance="tests/test_daemon.py",
    )

    facts = await daemon.process_memory_maintenance_once()

    node = GraphStore(tmp_path).get_by_name("MemoryItem", item.id)
    assert facts["ok"] is True
    assert facts["semantic_graph"]["synced_count"] == 1
    assert node is not None
    assert node.data["memory_id"] == item.id


@pytest.mark.asyncio
async def test_daemon_materializes_due_cron_goal_as_child_run(tmp_path: Path, monkeypatch) -> None:
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
    scheduled_for = time.time() - 1.0
    GoalStore(tmp_path).update_cron_run(registered.goal.id, scheduled_for)
    daemon = SystemDaemon(tmp_path, project_dir=tmp_path)

    async def no_memory_maintenance():
        return {"ok": True}

    async def no_events():
        return []

    monkeypatch.setattr(daemon, "process_memory_maintenance_once", no_memory_maintenance)
    monkeypatch.setattr(daemon, "process_events_once", no_events)

    created = await daemon.process_background_once()

    assert len(created) == 1
    assert created[0]["cron_goal_id"] == registered.goal.id
    assert created[0]["peer_id"] == "peer-1"
    assert created[0]["surface"] is False
    child = GoalStore(tmp_path).get(created[0]["goal_id"])
    assert child is not None
    assert child.parent_goal_id == registered.goal.id
    assert child.cron_schedule == ""
    child_loop = LoopRunStore(tmp_path).list_by_goal(child.id, limit=1)[0]
    assert child_loop.evidence["execution_mode"] == "background"
    refreshed = GoalStore(tmp_path).get(registered.goal.id)
    assert refreshed is not None
    assert refreshed.next_run_at > scheduled_for


def test_daemon_releases_orphaned_loop_resource_reservations(tmp_path: Path) -> None:
    service = LoopControlService(tmp_path)
    opened = service.open_goal(
        OpenGoalRequest(
            objective="recover durable resource reservation",
            workspace=str(tmp_path),
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            auto_start=False,
            execution_mode="background",
        )
    )
    ledger = SQLiteResourceLedger(tmp_path)
    gateway = GlobalResourceGateway(
        ResourceLimits(max_concurrent=1),
        ledger=ledger,
        scope_id=opened.loop_run.run_id,
    )
    grant = gateway.request(ResourceRequest(kind="llm:planner"))
    assert grant.allowed
    assert ledger.usage(opened.loop_run.run_id).active == 1

    SystemDaemon(tmp_path, project_dir=tmp_path)

    assert ledger.usage(opened.loop_run.run_id).active == 0


@pytest.mark.asyncio
async def test_daemon_reconciles_resource_reservations_after_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = SystemDaemon(tmp_path, project_dir=tmp_path)
    opened = LoopControlService(tmp_path).open_goal(
        OpenGoalRequest(
            objective="recover reservation created after daemon startup",
            workspace=str(tmp_path),
            auto_start=False,
            execution_mode="background",
        )
    )
    ledger = SQLiteResourceLedger(tmp_path)
    grant = GlobalResourceGateway(
        ResourceLimits(max_concurrent=1),
        ledger=ledger,
        scope_id=opened.loop_run.run_id,
    ).request(ResourceRequest(kind="llm:planner"))
    assert grant.allowed
    monkeypatch.setattr(
        LoopRunStore,
        "claim_active_for_execution_mode",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        LoopRunStore,
        "list_retryable_pauses",
        lambda *args, **kwargs: [],
    )

    assert await daemon.process_queue_once() == []
    assert ledger.usage(opened.loop_run.run_id).active == 0


def test_daemon_releases_expired_foreground_execution_lease(tmp_path: Path) -> None:
    service = LoopControlService(tmp_path)
    opened = service.open_goal(
        OpenGoalRequest(
            objective="recover expired foreground lease",
            workspace=str(tmp_path),
            auto_start=False,
            execution_mode="foreground",
        )
    )
    claimed = LoopRunStore(tmp_path).claim_for_execution(
        opened.loop_run.run_id,
        owner="stalled-worker",
        lease_seconds=1,
        now=1.0,
    )
    assert claimed is not None

    SystemDaemon(tmp_path, project_dir=tmp_path)

    recovered = LoopRunStore(tmp_path).get_run(opened.loop_run.run_id)
    assert recovered is not None
    assert recovered.lease_owner == ""
    assert recovered.lease_expires_at == 0.0


def test_daemon_releases_future_lease_owned_by_dead_daemon_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = LoopControlService(tmp_path).open_goal(
        OpenGoalRequest(
            objective="recover dead daemon ownership",
            workspace=str(tmp_path),
            auto_start=False,
            execution_mode="background",
        )
    )
    store = LoopRunStore(tmp_path)
    claimed = store.claim_for_execution(
        opened.loop_run.run_id,
        owner="daemon:999999:old-process",
        lease_seconds=10_000,
    )
    assert claimed is not None
    monkeypatch.setattr(
        "navi.daemon._execution_owner_process_is_alive",
        lambda _owner: False,
    )

    SystemDaemon(tmp_path, project_dir=tmp_path)

    recovered = store.get_run(opened.loop_run.run_id)
    assert recovered is not None
    assert recovered.lease_owner == ""
    assert recovered.lease_expires_at == 0.0


def test_daemon_releases_future_lease_owned_by_dead_state_graph_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = LoopControlService(tmp_path).open_goal(
        OpenGoalRequest(
            objective="recover dead foreground execution",
            workspace=str(tmp_path),
            auto_start=False,
            execution_mode="foreground",
        )
    )
    store = LoopRunStore(tmp_path)
    claimed = store.claim_for_execution(
        opened.loop_run.run_id,
        owner="state-graph:999999:old-process",
        lease_seconds=10_000,
    )
    assert claimed is not None
    monkeypatch.setattr(
        "navi.daemon._execution_owner_process_is_alive",
        lambda _owner: False,
    )

    SystemDaemon(tmp_path, project_dir=tmp_path)

    recovered = store.get_run(opened.loop_run.run_id)
    assert recovered is not None
    assert recovered.lease_owner == ""
    assert recovered.lease_expires_at == 0.0


@pytest.mark.asyncio
async def test_daemon_projects_transport_failure_and_releases_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = LoopControlService(tmp_path).open_goal(
        OpenGoalRequest(
            objective="fetch a current fact",
            workspace=str(tmp_path),
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            execution_mode="background",
        )
    )
    provider = _TransportFailingProvider()
    monkeypatch.setattr("navi.provider.build_provider", lambda _config: provider)

    processed = await SystemDaemon(tmp_path, project_dir=tmp_path).process_queue_once()
    assert [run.id for run in processed] == [opened.run.id]

    loop_run = LoopRunStore(tmp_path).get_run(opened.loop_run.run_id)
    assert loop_run is not None
    assert str(loop_run.terminal_state) == "paused"
    assert loop_run.evidence["tool"] == "system.planner_error"
    assert loop_run.evidence["args"]["error_type"] == "ReadError"
    assert loop_run.evidence["args"]["retryable"] is True
    assert loop_run.evidence["args"]["retry_after_seconds"] == 15.0
    assert loop_run.evidence["automatic_model_retry"] is False
    assert loop_run.evidence["retry_gate"]["kind"] == "provider_transport"
    assert loop_run.evidence["retry_gate"]["retry_count"] == 1
    goal = GoalStore(tmp_path).get(opened.goal.id)
    assert goal is not None
    assert goal.phase == "running"
    assert goal.resolution == "blocked"
    assert SQLiteResourceLedger(tmp_path).usage(opened.loop_run.run_id).active == 0
    assert provider.calls == 1

    with connect(LoopRunStore(tmp_path).db_path) as conn:
        conn.execute(
            "UPDATE loop_runs SET updated_at = 0 WHERE id = ?",
            (opened.loop_run.run_id,),
        )
    processed = await SystemDaemon(tmp_path, project_dir=tmp_path).process_queue_once()
    assert [run.id for run in processed] == [opened.run.id]

    exhausted = LoopRunStore(tmp_path).get_run(opened.loop_run.run_id)
    assert exhausted is not None
    assert str(exhausted.terminal_state) == "failed"
    assert exhausted.evidence["retry_gate"]["retry_count"] == 1
    assert exhausted.evidence["reflection"]["facts"]["transport_retry_exhausted"] is True
    goal = GoalStore(tmp_path).get(opened.goal.id)
    assert goal is not None
    assert goal.phase == "ended"
    assert goal.resolution == "failed"
    assert SQLiteResourceLedger(tmp_path).usage(opened.loop_run.run_id).active == 0
    assert provider.calls == 2
    evaluation = TraceStore(tmp_path).list_evaluations(opened.run.id)
    assert len(evaluation) == 1
    assert evaluation[0].outcome == "failure"
    assert evaluation[0].failure_domain == "planner_or_parser"


@pytest.mark.asyncio
async def test_daemon_durably_recovers_one_provider_transport_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = LoopControlService(tmp_path).open_goal(
        OpenGoalRequest(
            objective="answer after a transient provider reset",
            workspace=str(tmp_path),
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            execution_mode="background",
        )
    )
    provider = _TransportRecoveringProvider()
    monkeypatch.setattr("navi.provider.build_provider", lambda _config: provider)

    await SystemDaemon(tmp_path, project_dir=tmp_path).process_queue_once()
    paused = LoopRunStore(tmp_path).get_run(opened.loop_run.run_id)
    assert paused is not None
    assert str(paused.terminal_state) == "paused"
    assert provider.planner_calls == 1

    with connect(LoopRunStore(tmp_path).db_path) as conn:
        conn.execute(
            "UPDATE loop_runs SET updated_at = 0 WHERE id = ?",
            (opened.loop_run.run_id,),
        )
    await SystemDaemon(tmp_path, project_dir=tmp_path).process_queue_once()

    recovered = LoopRunStore(tmp_path).get_run(opened.loop_run.run_id)
    assert recovered is not None
    assert str(recovered.terminal_state) == "converged"
    assert recovered.evidence["retry_gate"]["retry_count"] == 1
    assert provider.planner_calls == 2
    goal = GoalStore(tmp_path).get(opened.goal.id)
    assert goal is not None
    assert goal.phase == "running"
    assert goal.resolution == "none"
    pending = GoalStore(tmp_path).accepted_result_for_run(opened.run.id)
    assert pending["delivery_status"] == "pending"
    assert pending["body"] == "recovered after transport retry"


@pytest.mark.asyncio
async def test_daemon_recovers_checker_transport_without_reexecuting_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = LoopControlService(tmp_path).open_goal(
        OpenGoalRequest(
            objective="preserve the candidate while checker transport recovers",
            workspace=str(tmp_path),
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            execution_mode="background",
        )
    )
    provider = _CheckerTransportRecoveringProvider()
    monkeypatch.setattr("navi.provider.build_provider", lambda _config: provider)

    await SystemDaemon(tmp_path, project_dir=tmp_path).process_queue_once()
    paused = LoopRunStore(tmp_path).get_run(opened.loop_run.run_id)
    assert paused is not None
    assert str(paused.terminal_state) == "paused"
    assert paused.evidence["retry_gate"]["model_role"] == "checker"
    assert paused.evidence["retry_gate"]["resume_node"] == "evaluate"
    assert provider.planner_calls == 1
    assert provider.checker_calls == 1

    with connect(LoopRunStore(tmp_path).db_path) as conn:
        conn.execute(
            "UPDATE loop_runs SET updated_at = 0 WHERE id = ?",
            (opened.loop_run.run_id,),
        )
    await SystemDaemon(tmp_path, project_dir=tmp_path).process_queue_once()

    recovered = LoopRunStore(tmp_path).get_run(opened.loop_run.run_id)
    assert recovered is not None
    assert str(recovered.terminal_state) == "converged"
    assert provider.planner_calls == 1
    assert provider.checker_calls == 2
    pending = GoalStore(tmp_path).accepted_result_for_run(opened.run.id)
    assert pending["delivery_status"] == "pending"
    assert pending["body"] == "checker retry kept this candidate"


@pytest.mark.asyncio
async def test_daemon_advances_and_traces_failed_cron_occurrence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / ".navi"
    vanished = tmp_path / "vanished"
    vanished.mkdir()
    service = LoopControlService(home)
    registered = service.open_goal(
        OpenGoalRequest(
            objective="daily reminder with missing workspace",
            workspace=str(vanished),
            loop_kind="scheduled",
            cron_schedule="15 8 * * *",
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            allowed_capabilities=("respond",),
        )
    )
    scheduled_for = time.time() - 1.0
    GoalStore(home).update_cron_run(registered.goal.id, scheduled_for)
    vanished.rmdir()
    daemon = SystemDaemon(home, project_dir=tmp_path)

    async def no_memory_maintenance():
        return {"ok": True}

    async def no_events():
        return []

    monkeypatch.setattr(daemon, "process_memory_maintenance_once", no_memory_maintenance)
    monkeypatch.setattr(daemon, "process_events_once", no_events)

    created = await daemon.process_background_once()

    assert len(created) == 1
    assert created[0]["surface"] is True
    assert created[0]["facts"]["kind"] == "scheduled_occurrence_failed"
    assert created[0]["facts"]["scheduled_for_iso"]
    assert created[0]["facts"]["next_run_at_iso"]
    refreshed = GoalStore(home).get(registered.goal.id)
    assert refreshed is not None
    assert refreshed.next_run_at > scheduled_for
    failure_events = [
        event
        for event in GoalStore(home).list_events(registered.goal.id)
        if event.event_type == "goal.schedule_occurrence_failed"
    ]
    assert len(failure_events) == 1
    trace_events = TraceStore(home).list_events(created[0]["trace_id"])
    assert len(trace_events) == 1
    assert trace_events[0].tool == "goal.open_scheduled_occurrence"
    assert trace_events[0].ok is False

    assert await daemon.process_background_once() == []


@pytest.mark.asyncio
async def test_daemon_blocks_invalid_persisted_cron_without_guessed_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / ".navi"
    registered = LoopControlService(home).open_goal(
        OpenGoalRequest(
            objective="persisted invalid schedule",
            workspace=str(tmp_path),
            loop_kind="scheduled",
            cron_schedule="0 8 * * *",
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            allowed_capabilities=("respond",),
        )
    )
    store = GoalStore(home)
    with connect(store.db_path) as conn:
        conn.execute(
            "UPDATE goals SET cron_schedule = ?, next_run_at = ? WHERE id = ?",
            ("invalid persisted value", 1.0, registered.goal.id),
        )
    daemon = SystemDaemon(home, project_dir=tmp_path)

    async def no_memory_maintenance():
        return {"ok": True}

    async def no_events():
        return []

    monkeypatch.setattr(daemon, "process_memory_maintenance_once", no_memory_maintenance)
    monkeypatch.setattr(daemon, "process_events_once", no_events)

    created = await daemon.process_background_once()

    assert len(created) == 1
    assert created[0]["facts"]["kind"] == "scheduled_template_invalid"
    assert created[0]["facts"]["state_transition"] == "schedule_blocked"
    refreshed = store.get(registered.goal.id)
    assert refreshed is not None
    assert refreshed.phase == "ended"
    assert refreshed.resolution == "blocked"
    assert refreshed.task_status == "blocked"
    assert refreshed.blocked_reason == "invalid_cron_schedule"
    assert refreshed.next_run_at == 0.0
    assert [event.event_type for event in store.list_events(registered.goal.id)].count(
        "goal.schedule_invalid"
    ) == 1
    assert await daemon.process_background_once() == []


@pytest.mark.asyncio
async def test_daemon_does_not_execute_foreground_or_manual_loops(tmp_path: Path) -> None:
    service = LoopControlService(tmp_path)
    foreground = service.open_goal(
        OpenGoalRequest(
            objective="foreground turn",
            workspace=str(tmp_path),
            auto_start=False,
            execution_mode="foreground",
        )
    )
    manual = service.open_goal(
        OpenGoalRequest(
            objective="manual goal",
            workspace=str(tmp_path),
            auto_start=False,
        )
    )

    processed = await SystemDaemon(tmp_path, project_dir=tmp_path).process_queue_once()

    assert processed == []
    assert str(LoopRunStore(tmp_path).get_run(foreground.loop_run.run_id).node) == "plan"
    assert str(LoopRunStore(tmp_path).get_run(manual.loop_run.run_id).node) == "plan"


@pytest.mark.asyncio
async def test_daemon_recovers_stale_connector_foreground_loop_with_durable_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = LoopControlService(tmp_path).open_goal(
        OpenGoalRequest(
            objective="recover interrupted connector turn",
            workspace=str(tmp_path),
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            auto_start=False,
            execution_mode="foreground",
        )
    )
    store = LoopRunStore(tmp_path)
    with connect(store.db_path) as conn:
        conn.execute(
            "UPDATE loop_runs SET updated_at = ? WHERE id = ?",
            (time.time() - 120.0, opened.loop_run.run_id),
        )
    captured: dict[str, object] = {}

    async def capture_resume(**kwargs):
        captured.update(kwargs)
        return opened

    monkeypatch.setattr("navi.goal_state_graph.resume_goal_loop_run", capture_resume)

    processed = await SystemDaemon(tmp_path, project_dir=tmp_path).process_queue_once()

    assert [run.id for run in processed] == [opened.run.id]
    assert captured["loop_run_id"] == opened.loop_run.run_id
    assert captured["persist_result_delivery"] is True
