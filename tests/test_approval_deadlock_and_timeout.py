"""Regression tests for two coupled incidents:

1. connector_router returned a false "处理超时" after a fixed 120s wall clock even
   while the turn was still running. Fix: idle-timeout reset by heartbeats.
2. A run stuck in awaiting_approval with an expired code could neither be
   approved (code gone) nor deleted from remote (status != failed) — a dead end.
   Fix: remote-deletable expired/awaiting states, auto-archive of expired runs,
   and code re-issue on approve.
"""
from __future__ import annotations

import asyncio

import pytest

from navi.actions.delegation import DelegateDeleteCapability
from navi.capabilities_types import CapabilityContext
from navi.connector_router import ConnectorRouter, IDLE_TIMEOUT_SECONDS
from navi.connector_runtime import ConnectorMessage
from navi.control import ApprovalService, SurfaceContext
from navi.event_bus import EventBus, ResponseReadyEvent
from navi.runs import RunStore


def _message(text: str = "你好") -> ConnectorMessage:
    return ConnectorMessage(
        message_id="msg-x",
        peer_id="peer",
        sender_id="sender",
        text=text,
        source="weixin",
        session_alias_prefix="test",
    )


@pytest.mark.asyncio
async def test_router_does_not_timeout_while_heartbeats_arrive(tmp_path, monkeypatch):
    # Idle window shorter than the work, but heartbeats keep resetting it.
    monkeypatch.setattr("navi.connector_router.IDLE_TIMEOUT_SECONDS", 0.2)
    bus = EventBus()
    router = ConnectorRouter(tmp_path, bus)

    async def on_intent(event):
        # Work takes ~0.5s (> idle window) but we beat every 0.1s.
        for _ in range(5):
            await asyncio.sleep(0.1)
            await bus.send_heartbeat(event.correlation_id)
        await bus.send_response(
            ResponseReadyEvent(
                source_agent="main_agent",
                correlation_id=event.correlation_id,
                text="done late but alive",
            )
        )

    bus.subscribe("message_ingress", on_intent)
    result = await router.route(_message())
    assert result == "done late but alive"
    await bus.shutdown()


@pytest.mark.asyncio
async def test_router_times_out_on_true_silence(tmp_path, monkeypatch):
    monkeypatch.setattr("navi.connector_router.IDLE_TIMEOUT_SECONDS", 0.15)
    bus = EventBus()
    router = ConnectorRouter(tmp_path, bus)

    async def on_intent(event):
        # Simulate a stuck/crashed upstream: never responds, never beats.
        await asyncio.sleep(1.0)

    bus.subscribe("message_ingress", on_intent)
    result = await router.route(_message())
    assert result == "处理超时，请稍后重试。"
    await bus.shutdown()


def test_idle_timeout_default_is_sane():
    # Heartbeat interval (20s) must sit comfortably below the idle window.
    assert IDLE_TIMEOUT_SECONDS >= 60.0


# ─── Deadlock-side tests ───


def _ctx(tmp_path) -> SurfaceContext:
    return SurfaceContext(
        home=tmp_path,
        source="weixin",
        peer_id="peer",
        sender_id="sender",
        input_text="",
    )


def _awaiting_run_with_expired_code(tmp_path) -> tuple[RunStore, str, str]:
    runs = RunStore(tmp_path)
    task = runs.create(
        "查找简历",
        kind="delegation",
        source="weixin",
        peer_id="peer",
        sender_id="sender",
        workspace=str(tmp_path),
        status="awaiting_approval",
    )
    # Create an approval already past its TTL.
    approval = runs.create_approval(
        run_id=task.id, peer_id="peer", sender_id="sender", ttl_seconds=-1
    )
    return runs, task.id, approval.code


def test_archive_expired_moves_run_out_of_awaiting(tmp_path):
    runs, run_id, _code = _awaiting_run_with_expired_code(tmp_path)
    runs.archive_expired_approvals()
    task = runs.get(run_id)
    assert task is not None
    assert task.status == "expired"  # left the active awaiting_approval list


def test_approve_expired_code_reissues_new_code(tmp_path):
    runs, run_id, code = _awaiting_run_with_expired_code(tmp_path)
    runs.archive_expired_approvals()  # marks approval expired, run -> expired

    service = ApprovalService(tmp_path)
    res = service.resolve(
        decision="approve",
        selection="explicit_code",
        context=_ctx(tmp_path),
        code=code,
    )
    # Re-issue does not approve; it hands back a fresh code and resurrects the run.
    assert res.ok is False
    assert res.facts is not None
    assert res.facts.get("reason") == "approval_reissued"
    new_code = res.facts["approval"]["code"]
    assert new_code != code
    task = runs.get(run_id)
    assert task.status == "awaiting_approval"
    # The fresh code is live and approvable.
    approve = service.resolve(
        decision="approve",
        selection="explicit_code",
        context=_ctx(tmp_path),
        code=new_code,
    )
    assert approve.ok is True


# ─── Remote delete of the dead-end run ───


async def _remote_delete(tmp_path, run_id: str):
    cap = DelegateDeleteCapability(spec=None, home=tmp_path)
    ctx = CapabilityContext(home=tmp_path, peer_id="peer", sender_id="sender", source="weixin")
    return await cap.invoke(
        {"run_id": run_id, "reason": "user asked to clear stuck task"},
        permission="write",
        context=ctx,
    )


@pytest.mark.asyncio
async def test_remote_can_delete_awaiting_approval_run(tmp_path):
    runs = RunStore(tmp_path)
    task = runs.create(
        "查找简历",
        kind="delegation",
        source="weixin",
        peer_id="peer",
        sender_id="sender",
        workspace=str(tmp_path),
        status="awaiting_approval",
    )
    res = await _remote_delete(tmp_path, task.id)
    assert res.ok is True
    assert runs.get(task.id) is None


@pytest.mark.asyncio
async def test_remote_can_delete_expired_run(tmp_path):
    runs, run_id, _code = _awaiting_run_with_expired_code(tmp_path)
    runs.archive_expired_approvals()
    assert runs.get(run_id).status == "expired"
    res = await _remote_delete(tmp_path, run_id)
    assert res.ok is True
    assert runs.get(run_id) is None


@pytest.mark.asyncio
async def test_remote_still_blocks_deleting_running_run(tmp_path):
    runs = RunStore(tmp_path)
    task = runs.create(
        "运行中",
        kind="delegation",
        source="weixin",
        peer_id="peer",
        sender_id="sender",
        workspace=str(tmp_path),
        status="running",
    )
    res = await _remote_delete(tmp_path, task.id)
    assert res.ok is False
    assert runs.get(task.id) is not None



