from __future__ import annotations

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.lifecycle import Phase


@pytest.mark.asyncio
async def test_delegate_run_returns_background_queue_facts(tmp_path):
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        peer_id="peer-1",
        sender_id="sender-1",
        source="cli",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )
    spawned = await registry.invoke(
        "delegate.spawn",
        {
            "objective": "queue background task",
            "context": "test",
            "plan": "test",
            "success_criteria": "test",
        },
        permission="prepare",
        context=context,
    )

    queued = await registry.invoke(
        "delegate.run",
        {"run_id": spawned.run_id},
        permission="prepare",
        context=context,
    )

    assert queued.ok is True
    assert queued.message == ""
    assert queued.facts["phase"] == Phase.PENDING
    assert queued.facts["background_execution"] == "queued"
    assert queued.facts["queue_state"] == "queued_for_background_execution"
    assert queued.facts["completion_evidence"] is True


@pytest.mark.asyncio
async def test_delegate_state_returns_scoped_single_run_facts(tmp_path):
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        peer_id="peer-1",
        sender_id="sender-1",
        source="weixin",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )
    spawned = await registry.invoke(
        "delegate.spawn",
        {
            "objective": "inspect background task",
            "context": "test",
            "plan": "test",
            "success_criteria": "test",
        },
        permission="prepare",
        context=context,
    )

    state = await registry.invoke(
        "delegate.state",
        {"run_id": spawned.run_id},
        permission="read",
        context=context,
    )

    assert state.ok is True
    assert state.facts["entity_type"] == "delegation_run"
    assert state.facts["run_id"] == spawned.run_id
    assert state.facts["run"]["id"] == spawned.run_id

    other_sender = CapabilityContext(
        home=tmp_path,
        peer_id="peer-1",
        sender_id="sender-2",
        source="weixin",
        permission_ceiling="read",
        workspace=str(tmp_path),
    )
    blocked = await registry.invoke(
        "delegate.state",
        {"run_id": spawned.run_id},
        permission="read",
        context=other_sender,
    )

    assert blocked.ok is False
    assert blocked.error_reason == "not_found"
