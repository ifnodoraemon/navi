from __future__ import annotations

import pytest

from navi.auth import AuthInspector
from navi.capabilities import CapabilityContext, CapabilityRegistry
from navi.daemon import SystemDaemon
from navi.execution import ExecutionService
from navi.tasks import TaskStore
from navi.trust import TrustStore


@pytest.mark.asyncio
async def test_task_approval_execution_and_evolution_through_os_primitives(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    tasks = TaskStore(tmp_path)
    daemon = SystemDaemon(tmp_path)

    planned = await capabilities.invoke(
        "task.create",
        {"prompt": "improve the navi project"},
        permission="prepare",
        context=CapabilityContext(home=tmp_path, peer_id="peer", sender_id="sender", source="weixin"),
    )

    assert planned.ok is True
    task = tasks.get(planned.task_id)
    assert task is not None
    assert task.status == "awaiting_approval"
    approval = tasks.list_approvals()[0]

    approved = await capabilities.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval.code},
        permission="write",
        context=CapabilityContext(home=tmp_path, sender_id="sender"),
    )

    assert approved.facts["task_status"] == "queued"
    completed = await daemon.process_queue_once()

    assert completed[0].status == "completed"
    assert tasks.get(planned.task_id).result_summary
    assert daemon.evolution.ledger.list()


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
    watches = TaskStore(tmp_path).list_watches()
    assert watches[0].cron == "*/5 * * * *"
    assert watches[0].prompt == "check the navi project"


@pytest.mark.asyncio
async def test_task_and_watch_delete_capabilities_remove_records(tmp_path):
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    store = TaskStore(tmp_path)
    task = store.create("delete me", status="failed")
    approval = store.create_approval(task_id=task.id, peer_id="peer", sender_id="sender")
    store.add_execution_log(
        task_id=task.id,
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
    )

    deleted_task = await capabilities.invoke(
        "task.delete",
        {"task_id": task.id},
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
    assert deleted_task.facts["task_id"] == task.id
    assert store.get(task.id) is None
    assert store.get_approval(approval.code) is None
    assert store.list_execution_logs(task.id) == []
    assert deleted_watch.ok is True
    assert store.list_watches() == []


def test_auth_inspector_shape():
    statuses = AuthInspector().status()

    assert {status.name for status in statuses} == {"codex", "gemini"}


@pytest.mark.asyncio
async def test_capability_manifest_and_execution_respect_permission_ceiling(tmp_path):
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path, permission_ceiling="read")

    names = {spec.name for spec in capabilities.planner_specs()}
    assert "final.answer" in names
    assert "service.status" in names
    assert "task.create" not in names
    assert "approval.resolve" not in names

    result = await capabilities.invoke(
        "task.create",
        {"prompt": "prepare local work"},
        permission="prepare",
        context=CapabilityContext(home=tmp_path, permission_ceiling="read"),
    )

    assert result.ok is False
    assert "capability not found" in result.message


def test_action_capabilities_are_loaded_from_manifest(tmp_path):
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)

    specs = {spec.name: spec for spec in capabilities.list_specs()}

    assert specs["task.create"].source == "action"
    assert specs["task.create"].permission == "prepare"
    assert specs["approval.resolve"].permission == "write"


def test_trust_success_does_not_auto_escalate_autonomy(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    trust = TrustStore(tmp_path)
    tasks = TaskStore(tmp_path)
    rule = trust.upsert(
        name="low risk",
        pattern="trusted",
        project_path="",
        sender_id="sender",
        autonomy_level="L2",
    )
    task = tasks.create(
        "trusted task",
        prompt="trusted task",
        sender_id="sender",
        trust_rule_id=rule.id,
        autonomy_level="L2",
        status="completed",
    )

    for _ in range(3):
        trust.record_success(task)

    assert trust.get(rule.id).autonomy_level == "L2"


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

    planned = await capabilities.invoke(
        "task.create",
        {"prompt": "trusted maintenance"},
        permission="prepare",
        context=CapabilityContext(home=tmp_path, peer_id="peer", sender_id="sender", source="weixin"),
    )

    task = TaskStore(tmp_path).get(planned.task_id)
    assert task.status == "queued"
    assert task.workspace == workspace
    completed = await SystemDaemon(tmp_path).process_queue_once()
    assert completed[0].status == "completed"


@pytest.mark.asyncio
async def test_codex_plan_timeout_marks_task_failed(tmp_path, monkeypatch):
    monkeypatch.delenv("NAVI_EXECUTION_MOCK", raising=False)
    monkeypatch.setenv("NAVI_EXECUTION_TIMEOUT_SECONDS", "1")
    execution = ExecutionService(tmp_path)
    task = TaskStore(tmp_path).create("timeout", status="pending")

    async def slow_plan(task):
        import asyncio

        await asyncio.sleep(2)

    monkeypatch.setattr(execution.providers["codex"], "plan", slow_plan)
    planned = await execution.plan_task(task)

    assert planned.status == "failed"
    assert "timed out" in planned.error


@pytest.mark.asyncio
async def test_evolution_rollback_restores_graph_event(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    tasks = TaskStore(tmp_path)
    daemon = SystemDaemon(tmp_path)
    planned = await capabilities.invoke(
        "task.create",
        {"prompt": "evolve rollback coverage"},
        permission="prepare",
        context=CapabilityContext(home=tmp_path, peer_id="peer", sender_id="sender", source="weixin"),
    )
    approval = tasks.list_approvals()[0]
    await capabilities.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval.code},
        permission="write",
        context=CapabilityContext(home=tmp_path, sender_id="sender"),
    )
    completed = (await daemon.process_queue_once())[0]

    events = daemon.evolution.ledger.list()
    assert {event.target_type for event in events} == {"graph_node"}

    for event in events:
        rolled_back = daemon.evolution.rollback(event.id)
        assert rolled_back is not None
        assert rolled_back.rolled_back_at

    assert daemon.evolution.ledger.list()[0].rolled_back_at
    assert completed.id
    assert planned.task_id
