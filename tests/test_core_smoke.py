from __future__ import annotations

from pathlib import Path

import pytest

from navi.app_factory import build_runtime
from navi.capabilities import CapabilityContext, CapabilityRegistry
from navi.engine import HernessEngine
from navi.execution import ExecutionService
from navi.goals import GoalStore
from navi.runs import RunStore


@pytest.mark.asyncio
async def test_core_chat_shutdown_returns_with_background_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("NAVI_MODEL", "mock")
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    engine = HernessEngine(home=tmp_path, runtime=build_runtime(tmp_path), project_dir=Path.cwd())

    result = await engine.handle("hello", peer_id="cli", sender_id="cli", source="cli")
    await engine.shutdown(timeout=1)

    assert result.session_id
    assert len(engine._background_tasks) == 0


@pytest.mark.asyncio
async def test_core_delegation_approval_execution_goal_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("NAVI_MODEL", "mock")
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    registry = CapabilityRegistry(home=tmp_path, project_dir=Path.cwd())
    context = CapabilityContext(
        home=tmp_path,
        peer_id="cli",
        sender_id="cli",
        source="cli",
        workspace=str(Path.cwd()),
    )

    spawned = await registry.invoke(
        "delegate.spawn",
        {"objective": "core smoke task", "context": "mock", "plan": "mock", "success_criteria": "mock"},
        permission="prepare",
        context=context,
    )
    assert spawned.ok is True
    run_id = spawned.run_id
    assert spawned.facts["entity_type"] == "delegation_run"
    assert spawned.facts["entity_id"] == run_id
    assert spawned.facts["state_transition"] == "created"
    assert spawned.facts["turn_scope"] == "current"

    prepared = await registry.invoke(
        "delegate.prepare",
        {"run_id": run_id},
        permission="prepare",
        context=context,
    )
    assert prepared.ok is True
    assert prepared.facts["status"] == "prepared"
    assert prepared.facts["entity_type"] == "delegation_run"
    assert prepared.facts["entity_id"] == run_id
    assert prepared.facts["state_transition"] == "updated"

    requested = await registry.invoke(
        "approval.request",
        {"run_id": run_id},
        permission="prepare",
        context=context,
    )
    assert requested.ok is True
    assert requested.facts["entity_type"] == "approval_request"
    assert requested.facts["state_transition"] == "created"

    approved = await registry.invoke(
        "approval.resolve",
        {"decision": "approve", "code": requested.facts["approval"]["code"]},
        permission="write",
        context=context,
    )
    assert approved.ok is True
    assert approved.facts["run_status"] == "queued"
    assert approved.facts["entity_type"] == "approval_request"
    assert approved.facts["state_transition"] == "updated"

    queued = await registry.invoke(
        "delegate.run",
        {"run_id": run_id},
        permission="write",
        context=context,
    )
    assert queued.ok is True
    assert queued.facts["entity_type"] == "delegation_run"
    assert queued.facts["state_transition"] == "updated"

    processed = await ExecutionService(tmp_path).process_pending_once(limit=5)
    task = RunStore(tmp_path).get(run_id)
    goal = GoalStore(tmp_path).get_by_run(run_id)

    assert [item.id for item in processed] == [run_id]
    assert task.status == "completed"
    assert task.result_summary
    assert goal is not None
    assert goal.status == "verified_complete"


@pytest.mark.asyncio
async def test_core_watch_create_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("NAVI_MODEL", "mock")
    registry = CapabilityRegistry(home=tmp_path, project_dir=Path.cwd())
    context = CapabilityContext(home=tmp_path, peer_id="cli", sender_id="cli", source="cli")

    result = await registry.invoke(
        "watch.create",
        {"cron": "*/5 * * * *", "prompt": "core watch"},
        permission="prepare",
        context=context,
    )

    assert result.ok is True
    assert result.facts["watch_id"]
    assert result.facts["entity_type"] == "watch"
    assert result.facts["entity_id"] == result.facts["watch_id"]
    assert result.facts["state_transition"] == "created"
    assert result.facts["turn_scope"] == "current"
    assert result.facts["next_run_at"] > 0
