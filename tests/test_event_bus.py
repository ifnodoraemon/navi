"""Tests for the event bus architecture: router → governance → response."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from navi.connector_router import ApprovalCodeDetector, ConnectorRouter
from navi.connector_runtime import ConnectorMessage
from navi.event_bus import (
    ActionApprovedEvent,
    ActionRequestedEvent,
    ActionSuspendedEvent,
    EventBus,
    ResponseReadyEvent,
    UserIntentEvent,
)
from navi.governance_agent import GovernanceAgent
from navi.runs import RunStore


@pytest.mark.asyncio
async def test_event_bus_publish_subscribe():
    bus = EventBus()
    received = []

    async def handler(event):
        received.append(event)

    bus.subscribe("user_intent", handler)
    event = UserIntentEvent(source_agent="test", text="hello")
    await bus.publish(event)
    assert len(received) == 1
    assert received[0].text == "hello"


@pytest.mark.asyncio
async def test_event_bus_response_channel():
    bus = EventBus()
    channel = bus.create_response_channel("corr-1")

    await bus.send_response(ResponseReadyEvent(
        source_agent="test",
        correlation_id="corr-1",
        text="response text",
    ))

    resp = await asyncio.wait_for(channel.get(), timeout=1.0)
    assert resp.text == "response text"
    bus.remove_response_channel("corr-1")


@pytest.mark.asyncio
async def test_event_bus_unsubscribe():
    bus = EventBus()
    received = []

    async def handler(event):
        received.append(event)

    unsub = bus.subscribe("user_intent", handler)
    unsub()

    await bus.publish(UserIntentEvent(source_agent="test", text="ignored"))
    assert len(received) == 0


def test_approval_code_detector_classifies_approval(tmp_path):
    runs = RunStore(tmp_path)
    task = runs.create("test task", workspace=str(tmp_path))
    approval = runs.create_approval(run_id=task.id, peer_id="p", sender_id="s")

    detector = ApprovalCodeDetector(tmp_path)

    result = detector.classify(f"批准 {approval.code}")
    assert result.kind == "approval_code"
    assert result.code == approval.code
    assert result.decision == "approve"

    result = detector.classify(f"reject {approval.code}")
    assert result.kind == "approval_code"
    assert result.decision == "reject"

    result = detector.classify("帮我搜一下天气")
    assert result.kind == "user_intent"


def test_approval_code_detector_bare_code(tmp_path):
    runs = RunStore(tmp_path)
    task = runs.create("test task", workspace=str(tmp_path))
    approval = runs.create_approval(run_id=task.id, peer_id="p", sender_id="s")

    detector = ApprovalCodeDetector(tmp_path)
    result = detector.classify(f"  {approval.code}  ")
    assert result.kind == "approval_code"
    assert result.decision == "approve"


def test_approval_code_detector_ignores_non_existent_code(tmp_path):
    detector = ApprovalCodeDetector(tmp_path)
    result = detector.classify("批准 999999")
    assert result.kind == "user_intent"


@pytest.mark.asyncio
async def test_connector_router_routes_user_intent(tmp_path):
    bus = EventBus()
    router = ConnectorRouter(tmp_path, bus)
    received = []

    async def on_intent(event):
        received.append(event)
        await bus.send_response(ResponseReadyEvent(
            source_agent="test_agent",
            correlation_id=event.correlation_id,
            text="handled",
        ))

    bus.subscribe("user_intent", on_intent)

    message = ConnectorMessage(
        message_id="msg-1",
        peer_id="peer",
        sender_id="sender",
        text="你好",
        source="test",
        session_alias_prefix="test",
    )
    result = await router.route(message)
    assert result == "handled"
    assert len(received) == 1
    assert received[0].text == "你好"


@pytest.mark.asyncio
async def test_connector_router_routes_approval_code(tmp_path):
    bus = EventBus()
    router = ConnectorRouter(tmp_path, bus)

    runs = RunStore(tmp_path)
    task = runs.create("test", workspace=str(tmp_path))
    approval = runs.create_approval(run_id=task.id, peer_id="p", sender_id="s")

    received_codes = []

    async def on_code(event):
        received_codes.append(event)
        await bus.send_response(ResponseReadyEvent(
            source_agent="governance",
            correlation_id=event.correlation_id,
            text="approved",
        ))

    bus.subscribe("approval_code", on_code)

    message = ConnectorMessage(
        message_id="msg-2",
        peer_id="peer",
        sender_id="sender",
        text=f"批准 {approval.code}",
        source="test",
        session_alias_prefix="test",
    )
    result = await router.route(message)
    assert result == "approved"
    assert len(received_codes) == 1
    assert received_codes[0].code == approval.code


@pytest.mark.asyncio
async def test_governance_agent_approves_low_risk(tmp_path):
    bus = EventBus()
    governance = GovernanceAgent(tmp_path, bus)

    runs = RunStore(tmp_path)
    task = runs.create("low risk task", workspace=str(tmp_path), autonomy_level="L3")

    approved = []
    bus.subscribe("action_approved", lambda e: asyncio.ensure_future(_collect(approved, e)))

    async def _collect(lst, e):
        lst.append(e)

    await bus.publish(ActionRequestedEvent(
        source_agent="main_agent",
        run_id=task.id,
        peer_id="p",
        sender_id="s",
        source="cli",
    ))

    assert len(approved) == 1
    assert approved[0].run_id == task.id


@pytest.mark.asyncio
async def test_governance_agent_suspends_high_risk(tmp_path):
    bus = EventBus()
    governance = GovernanceAgent(tmp_path, bus)

    runs = RunStore(tmp_path)
    task = runs.create("high risk task", workspace=str(tmp_path), autonomy_level="L2")

    suspended = []

    async def on_suspended(e):
        suspended.append(e)

    bus.subscribe("action_suspended", on_suspended)

    channel = bus.create_response_channel("corr-test")

    await bus.publish(ActionRequestedEvent(
        source_agent="main_agent",
        correlation_id="corr-test",
        run_id=task.id,
        peer_id="p",
        sender_id="s",
        source="cli",
    ))

    assert len(suspended) == 1
    assert suspended[0].run_id == task.id
    assert len(suspended[0].approval_code) == 6

    resp = await asyncio.wait_for(channel.get(), timeout=1.0)
    assert "审批" in resp.text or suspended[0].approval_code in resp.text
    bus.remove_response_channel("corr-test")


@pytest.mark.asyncio
async def test_governance_agent_resolves_approval_code(tmp_path):
    from navi.event_bus import ApprovalCodeEvent, ApprovalResolvedEvent

    bus = EventBus()
    governance = GovernanceAgent(tmp_path, bus)

    runs = RunStore(tmp_path)
    task = runs.create("test", workspace=str(tmp_path))
    approval = runs.create_approval(run_id=task.id, peer_id="p", sender_id="s")

    resolved = []

    async def on_resolved(e):
        resolved.append(e)

    bus.subscribe("approval_resolved", on_resolved)

    channel = bus.create_response_channel("corr-resolve")
    await bus.publish(ApprovalCodeEvent(
        source_agent="router",
        correlation_id="corr-resolve",
        code=approval.code,
        decision="approve",
        peer_id="p",
        sender_id="s",
        source="cli",
    ))

    assert len(resolved) == 1
    assert resolved[0].run_id == task.id

    resp = await asyncio.wait_for(channel.get(), timeout=1.0)
    assert "批准" in resp.text or "已批准" in resp.text
    bus.remove_response_channel("corr-resolve")
