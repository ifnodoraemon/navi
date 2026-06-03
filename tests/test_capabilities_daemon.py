from __future__ import annotations

import sqlite3

import pytest

from navi.auth import AuthInspector
from navi.capabilities import CapabilityContext, CapabilityRegistry
from navi.daemon import SystemDaemon
from navi.execution import ExecutionService
from navi.goals import GOAL_STATUS_ACTIVE, GOAL_STATUS_AWAITING_APPROVAL, GOAL_STATUS_VERIFIED_COMPLETE, GoalStore
from navi.runs import RunStore
from navi.trust import TrustStore


def test_run_store_rejects_watch_schema_drift(tmp_path):
    with sqlite3.connect(tmp_path / "runs.db") as conn:
        conn.execute(
            """
            CREATE TABLE watches (
                id TEXT PRIMARY KEY,
                cron TEXT NOT NULL,
                prompt TEXT NOT NULL,
                peer_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                next_run_at REAL NOT NULL,
                last_run_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                workspace TEXT NOT NULL
            )
            """
        )

    with pytest.raises(RuntimeError, match="watches schema mismatch"):
        RunStore(tmp_path)


async def _record_prepare_request(capabilities, tmp_path, prompt, *, context=None):
    context = context or CapabilityContext(home=tmp_path, peer_id="peer", sender_id="sender", source="weixin")
    recorded = await capabilities.invoke(
        "delegate.spawn",
        {"prompt": prompt},
        permission="prepare",
        context=context,
    )
    assert recorded.ok is True
    prepared = await capabilities.invoke(
        "delegate.prepare",
        {"run_id": recorded.run_id},
        permission="prepare",
        context=context,
    )
    assert prepared.ok is True
    requested = await capabilities.invoke(
        "approval.request",
        {"run_id": recorded.run_id},
        permission="prepare",
        context=context,
    )
    assert requested.ok is True
    return requested


@pytest.mark.asyncio
async def test_task_approval_execution_and_evolution_through_os_primitives(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    runs = RunStore(tmp_path)
    daemon = SystemDaemon(tmp_path, project_dir=tmp_path)

    planned = await _record_prepare_request(capabilities, tmp_path, "improve the navi project")

    assert planned.ok is True
    task = runs.get(planned.run_id)
    assert task is not None
    assert task.status == "awaiting_approval"
    goal_store = GoalStore(tmp_path)
    goal = goal_store.get_by_run(task.id)
    assert goal is not None
    assert goal.status == GOAL_STATUS_AWAITING_APPROVAL
    approval = runs.list_approvals()[0]

    approved = await capabilities.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval.code},
        permission="write",
        context=CapabilityContext(home=tmp_path, sender_id="sender"),
    )

    assert approved.facts["run_status"] == "queued"
    queued_goal = goal_store.get_by_run(task.id)
    assert queued_goal is not None
    assert queued_goal.status == GOAL_STATUS_ACTIVE
    completed = await daemon.process_queue_once()

    assert completed[0].status == "completed"
    assert runs.get(planned.run_id).result_summary
    completed_goal = goal_store.get_by_run(task.id)
    assert completed_goal is not None
    assert completed_goal.status == GOAL_STATUS_VERIFIED_COMPLETE
    assert {event.event_type for event in goal_store.list_events(completed_goal.id)} >= {
        "goal.created",
        "goal.run_status",
    }
    assert daemon.evolution.ledger.list()


@pytest.mark.asyncio
async def test_delegate_spawn_uses_capability_context_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    process_cwd = tmp_path / "process-cwd"
    requested_workspace = tmp_path / "requested-workspace"
    process_cwd.mkdir()
    requested_workspace.mkdir()
    monkeypatch.chdir(process_cwd)
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=process_cwd)

    planned = await capabilities.invoke(
        "delegate.spawn",
        {"prompt": "inspect the requested workspace"},
        permission="prepare",
        context=CapabilityContext(
            home=tmp_path,
            peer_id="peer",
            sender_id="sender",
            source="daemon",
            workspace=str(requested_workspace),
        ),
    )

    task = RunStore(tmp_path).get(planned.run_id)
    assert task is not None
    assert task.workspace == str(requested_workspace.resolve())
    assert str(process_cwd.resolve()) != task.workspace


@pytest.mark.asyncio
async def test_watch_create_uses_capability_context_workspace(tmp_path, monkeypatch):
    process_cwd = tmp_path / "process-cwd"
    requested_workspace = tmp_path / "requested-workspace"
    process_cwd.mkdir()
    requested_workspace.mkdir()
    monkeypatch.chdir(process_cwd)
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=process_cwd)

    result = await capabilities.invoke(
        "watch.create",
        {"cron": "*/5 * * * *", "prompt": "check requested workspace"},
        permission="prepare",
        context=CapabilityContext(
            home=tmp_path,
            peer_id="peer",
            sender_id="sender",
            source="daemon",
            workspace=str(requested_workspace),
        ),
    )

    assert result.ok is True
    watch = RunStore(tmp_path).list_watches()[0]
    assert watch.workspace == str(requested_workspace.resolve())
    assert str(process_cwd.resolve()) != watch.workspace


@pytest.mark.asyncio
async def test_delegation_lifecycle_can_be_model_selected_step_by_step(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    runs = RunStore(tmp_path)

    recorded = await capabilities.invoke(
        "delegate.spawn",
        {"prompt": "stepwise task"},
        permission="prepare",
        context=CapabilityContext(home=tmp_path, peer_id="peer", sender_id="sender", workspace=str(tmp_path)),
    )
    prepared = await capabilities.invoke(
        "delegate.prepare",
        {"run_id": recorded.run_id},
        permission="prepare",
        context=CapabilityContext(home=tmp_path, peer_id="peer", sender_id="sender"),
    )
    requested = await capabilities.invoke(
        "approval.request",
        {"run_id": recorded.run_id},
        permission="prepare",
        context=CapabilityContext(home=tmp_path, peer_id="peer", sender_id="sender"),
    )
    approval = runs.list_approvals()[0]
    runs.resolve_approval(approval.code, "sender", "approved")
    queued = await capabilities.invoke(
        "delegate.run",
        {"run_id": recorded.run_id},
        permission="write",
        context=CapabilityContext(home=tmp_path, peer_id="peer", sender_id="sender"),
    )

    assert recorded.ok is True
    assert prepared.facts["status"] == "prepared"
    assert requested.facts["status"] == "awaiting_approval"
    assert queued.facts["status"] == "queued"


@pytest.mark.asyncio
async def test_approval_resolve_reports_missing_task_approval(tmp_path):
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    task = RunStore(tmp_path).create("orphan task", status="preparing", workspace=str(tmp_path))

    result = await capabilities.invoke(
        "approval.resolve",
        {"decision": "reject", "run_id": task.id},
        permission="write",
        context=CapabilityContext(home=tmp_path, sender_id="sender"),
    )

    assert result.ok is False
    assert result.message == "Run has no approval request."
    assert result.facts["approval_resolution"]["reason"] == "run_has_no_approval"
    assert result.facts["approval_resolution"]["run_status"] == "preparing"


@pytest.mark.asyncio
async def test_approval_resolve_reports_sender_mismatch(tmp_path):
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    store = RunStore(tmp_path)
    task = store.create("owned task", workspace=str(tmp_path))
    approval = store.create_approval(run_id=task.id, peer_id="peer", sender_id="owner")

    result = await capabilities.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval.code},
        permission="write",
        context=CapabilityContext(home=tmp_path, sender_id="other"),
    )

    assert result.ok is False
    assert result.message == "Approval exists but belongs to a different sender."
    assert result.facts["approval_resolution"]["reason"] == "sender_mismatch"
    assert result.facts["approval_resolution"]["sender_matches"] is False


@pytest.mark.asyncio
async def test_approval_resolve_reports_consumed_approval(tmp_path):
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    store = RunStore(tmp_path)
    task = store.create("approved task", workspace=str(tmp_path))
    approval = store.create_approval(run_id=task.id, peer_id="peer", sender_id="sender")
    store.resolve_approval(approval.code, "sender", "approved")

    result = await capabilities.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval.code},
        permission="write",
        context=CapabilityContext(home=tmp_path, sender_id="sender"),
    )

    assert result.ok is False
    assert result.message == "Approval is not pending; current status is approved."
    assert result.facts["approval_resolution"]["reason"] == "approval_not_pending"


@pytest.mark.asyncio
async def test_watch_capability_creates_cron_watch(tmp_path):
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)

    result = await capabilities.invoke(
        "watch.create",
        {"cron": "*/5 * * * *", "prompt": "check the navi project"},
        permission="prepare",
        context=CapabilityContext(home=tmp_path, peer_id="peer", sender_id="sender"),
    )

    assert result.ok is True
    watches = RunStore(tmp_path).list_watches()
    assert watches[0].cron == "*/5 * * * *"
    assert watches[0].prompt == "check the navi project"
    assert watches[0].kind == "recurring"


@pytest.mark.asyncio
async def test_watch_capability_creates_one_shot_watch(tmp_path):
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)

    result = await capabilities.invoke(
        "watch.create",
        {"kind": "once", "run_at_text": "15:30", "prompt": "pmp related knowledge"},
        permission="prepare",
        context=CapabilityContext(home=tmp_path, peer_id="peer", sender_id="sender"),
    )

    assert result.ok is True
    watches = RunStore(tmp_path).list_watches()
    assert watches[0].cron == "once"
    assert watches[0].kind == "once"
    assert watches[0].prompt == "pmp related knowledge"
    assert watches[0].next_run_at > 0


@pytest.mark.asyncio
async def test_task_and_watch_delete_capabilities_remove_records(tmp_path):
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    store = RunStore(tmp_path)
    task = store.create("delete me", status="failed", workspace=str(tmp_path))
    approval = store.create_approval(run_id=task.id, peer_id="peer", sender_id="sender")
    store.add_execution_log(
        run_id=task.id,
        provider="mock",
        phase="execute",
        command="mock",
        stdout="",
        stderr="",
        exit_code=1,
        started_at=1,
        ended_at=2,
    )
    watch = store.create_watch(
        cron="0 20 * * *",
        prompt="delete watch",
        peer_id="peer",
        sender_id="sender",
        next_run_at=1,
        workspace=str(tmp_path),
    )

    deleted_task = await capabilities.invoke(
        "delegate.delete",
        {"run_id": task.id},
        permission="write",
        context=CapabilityContext(home=tmp_path),
    )
    deleted_watch = await capabilities.invoke(
        "watch.delete",
        {"watch_id": watch.id},
        permission="write",
        context=CapabilityContext(home=tmp_path),
    )

    assert deleted_task.ok is True
    assert deleted_task.facts["run_id"] == task.id
    assert store.get(task.id) is None
    assert store.get_approval(approval.code) is None
    assert store.list_execution_logs(task.id) == []
    assert deleted_watch.ok is True
    assert store.list_watches() == []


@pytest.mark.asyncio
async def test_delegate_delete_can_cleanup_failed_delegations_by_filter(tmp_path):
    store = RunStore(tmp_path)
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    failed_watch = store.create("old watch residue", status="failed", kind="watch", source="watch", workspace=str(tmp_path))
    failed_watch_2 = store.create("old watch residue 2", status="failed", kind="watch", source="watch", workspace=str(tmp_path))
    failed_manual = store.create("manual failed", status="failed", kind="manual", source="local", workspace=str(tmp_path))
    queued = store.create("still queued", status="queued", kind="manual", source="local", workspace=str(tmp_path))

    result = await capabilities.invoke(
        "delegate.delete",
        {"source": "watch"},
        permission="write",
        context=CapabilityContext(home=tmp_path, source="connector.weixin"),
    )

    assert result.ok is True
    assert result.facts["before_count"] == 2
    assert result.facts["deleted_count"] == 2
    assert result.facts["remaining_count"] == 0
    assert result.facts["cleanup_complete"] is True
    assert {task["run_id"] for task in result.facts["deleted_runs"]} == {failed_watch.id, failed_watch_2.id}
    assert store.get(failed_watch.id) is None
    assert store.get(failed_watch_2.id) is None
    assert store.get(failed_manual.id) is not None
    assert store.get(queued.id) is not None
    logs = store.list_tool_call_logs()
    assert logs[0].tool == "delegate.delete"
    assert logs[0].ok is True


@pytest.mark.asyncio
async def test_delegate_delete_reports_partial_cleanup_when_limited(tmp_path):
    store = RunStore(tmp_path)
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    for index in range(3):
        store.create(f"failed {index}", status="failed", kind="delegation", source="watch", workspace=str(tmp_path))

    result = await capabilities.invoke(
        "delegate.delete",
        {"source": "watch", "status": "failed", "limit": 1},
        permission="write",
        context=CapabilityContext(home=tmp_path, source="weixin"),
    )

    assert result.ok is True
    assert result.facts["deleted_count"] == 1
    assert result.facts["remaining_count"] == 2
    assert result.facts["cleanup_complete"] is False
    assert store.count_runs(status="failed", source="watch") == 2


@pytest.mark.asyncio
async def test_remote_delegate_delete_rejects_non_failed_single_task(tmp_path):
    store = RunStore(tmp_path)
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    task = store.create("active remote delete should be blocked", status="queued", workspace=str(tmp_path))

    result = await capabilities.invoke(
        "delegate.delete",
        {"run_id": task.id},
        permission="write",
        context=CapabilityContext(home=tmp_path, source="connector.weixin"),
    )

    assert result.ok is False
    assert result.message == "remote delegate.delete can only delete failed delegation runs."
    assert store.get(task.id) is not None


def test_auth_inspector_shape():
    statuses = AuthInspector().status()

    assert {status.name for status in statuses} >= {"codex", "qwen", "claude", "gemini"}


@pytest.mark.asyncio
async def test_capability_manifest_and_execution_respect_permission_ceiling(tmp_path):
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path, permission_ceiling="read")

    names = {spec.name for spec in capabilities.planner_specs()}
    assert "final.answer" in names
    assert "service.status" in names
    assert "delegate.spawn" not in names
    assert "approval.resolve" not in names

    result = await capabilities.invoke(
        "delegate.spawn",
        {"prompt": "prepare local work"},
        permission="prepare",
        context=CapabilityContext(home=tmp_path, permission_ceiling="read"),
    )

    assert result.ok is False
    assert "capability not found" in result.message


@pytest.mark.asyncio
async def test_tools_list_reflects_permission_ceiling(tmp_path):
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path, permission_ceiling="read")

    result = await capabilities.invoke(
        "tools.list",
        {},
        permission="read",
        context=CapabilityContext(home=tmp_path, permission_ceiling="read"),
    )

    names = {item["name"] for item in result.facts["tools"]}
    assert "tools.list" in names
    assert "delegate.spawn" not in names
    assert "file.write" not in names
    assert "browser.screenshot" not in names


def test_action_capabilities_are_loaded_from_manifest(tmp_path):
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)

    specs = {spec.name: spec for spec in capabilities.list_specs()}

    assert specs["delegate.spawn"].source == "action"
    assert specs["delegate.spawn"].permission == "prepare"
    assert specs["approval.resolve"].permission == "write"


def test_capability_graph_unifies_actions_and_gateway_tools(tmp_path):
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)

    graph = {node.name: node for node in capabilities.capability_graph()}

    assert graph["delegate.spawn"].provider == "action"
    assert graph["delegate.spawn"].mutates is True
    assert graph["provider.config"].provider == "tool_gateway"
    assert graph["provider.config"].facts_only is True
    assert {"action", "core", "connector.weixin", "connector.telegram"} <= set(capabilities.list_sources())


@pytest.mark.asyncio
async def test_mutating_action_capabilities_are_audited_once(tmp_path):
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    store = RunStore(tmp_path)

    result = await capabilities.invoke(
        "delegate.spawn",
        {"prompt": "audit task recording"},
        permission="prepare",
        context=CapabilityContext(home=tmp_path, peer_id="peer", sender_id="sender"),
    )

    assert result.ok is True
    logs = store.list_tool_call_logs()
    assert [log.tool for log in logs] == ["delegate.spawn"]
    assert logs[0].ok is True


@pytest.mark.asyncio
async def test_gateway_capability_audit_is_not_duplicated(tmp_path):
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    store = RunStore(tmp_path)

    result = await capabilities.invoke(
        "provider.config",
        {},
        permission="read",
        context=CapabilityContext(home=tmp_path),
    )

    assert result.ok is True
    logs = store.list_tool_call_logs()
    assert [log.tool for log in logs] == ["provider.config"]


def test_trust_success_does_not_auto_escalate_autonomy(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    trust = TrustStore(tmp_path)
    runs = RunStore(tmp_path)
    rule = trust.upsert(
        name="low risk",
        pattern="trusted",
        project_path="",
        sender_id="sender",
        autonomy_level="L2",
    )
    task = runs.create(
        "trusted task",
        prompt="trusted task",
        sender_id="sender",
        trust_rule_id=rule.id,
        autonomy_level="L2",
        status="completed",
        workspace=str(tmp_path),
    )

    for _ in range(2):
        trust.record_success(task)
    assert trust.get(rule.id).autonomy_level == "L2"

    trust.record_success(task)
    assert trust.get(rule.id).autonomy_level == "L3"


@pytest.mark.asyncio
async def test_explicit_l3_trust_rule_can_auto_execute(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    monkeypatch.chdir(tmp_path)
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    trust = TrustStore(tmp_path)
    workspace = str(tmp_path.resolve())
    trust.upsert(
        name="explicit trusted workspace",
        pattern="trusted",
        project_path=workspace,
        sender_id="sender",
        autonomy_level="L3",
    )

    recorded = await capabilities.invoke(
        "delegate.spawn",
        {"prompt": "trusted maintenance"},
        permission="prepare",
        context=CapabilityContext(home=tmp_path, peer_id="peer", sender_id="sender", source="weixin"),
    )
    await capabilities.invoke(
        "delegate.prepare",
        {"run_id": recorded.run_id},
        permission="prepare",
        context=CapabilityContext(home=tmp_path, peer_id="peer", sender_id="sender", source="weixin"),
    )
    planned = await capabilities.invoke(
        "delegate.run",
        {"run_id": recorded.run_id},
        permission="write",
        context=CapabilityContext(home=tmp_path, peer_id="peer", sender_id="sender", source="weixin"),
    )

    task = RunStore(tmp_path).get(planned.run_id)
    assert task.status == "queued"
    assert task.workspace == workspace
    completed = await SystemDaemon(tmp_path, project_dir=tmp_path).process_queue_once()
    assert completed[0].status == "completed"


@pytest.mark.asyncio
async def test_internal_plan_timeout_marks_task_failed(tmp_path, monkeypatch):
    monkeypatch.delenv("NAVI_EXECUTION_MOCK", raising=False)
    monkeypatch.setenv("NAVI_EXECUTION_TIMEOUT_SECONDS", "1")
    execution = ExecutionService(tmp_path)
    task = RunStore(tmp_path).create("timeout", status="pending", workspace=str(tmp_path))

    async def slow_plan(task):
        import asyncio

        await asyncio.sleep(2)

    monkeypatch.setattr(execution.provider, "plan", slow_plan)
    planned = await execution.plan_task(task)

    assert planned.status == "failed"
    assert "timed out" in planned.error


@pytest.mark.asyncio
async def test_evolution_rollback_restores_graph_event(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    runs = RunStore(tmp_path)
    daemon = SystemDaemon(tmp_path, project_dir=tmp_path)
    planned = await _record_prepare_request(capabilities, tmp_path, "evolve rollback coverage")
    approval = runs.list_approvals()[0]
    await capabilities.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval.code},
        permission="write",
        context=CapabilityContext(home=tmp_path, sender_id="sender"),
    )
    completed = (await daemon.process_queue_once())[0]

    events = daemon.evolution.ledger.list()
    assert {event.target_type for event in events}.issubset({"graph_node", "run_execution"})

    for event in events:
        rolled_back = daemon.evolution.rollback(event.id)
        assert rolled_back is not None
        assert rolled_back.rolled_back_at

    assert daemon.evolution.ledger.list()[0].rolled_back_at
    assert completed.id
    assert planned.run_id
