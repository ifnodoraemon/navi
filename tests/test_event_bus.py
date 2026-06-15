"""Tests for the event bus architecture: ingress → model agent → governance facts."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from navi.connector_router import ConnectorRouter
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

    bus.subscribe("message_ingress", on_intent)

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
async def test_connector_router_sends_approval_text_to_user_intent(tmp_path):
    bus = EventBus()
    router = ConnectorRouter(tmp_path, bus)

    runs = RunStore(tmp_path)
    task = runs.create("test", workspace=str(tmp_path))
    approval = runs.create_approval(run_id=task.id, peer_id="p", sender_id="s")

    received_intents = []

    async def on_intent(event):
        received_intents.append(event)
        await bus.send_response(ResponseReadyEvent(
            source_agent="main_agent",
            correlation_id=event.correlation_id,
            text="planner handled approval text",
        ))

    bus.subscribe("message_ingress", on_intent)

    message = ConnectorMessage(
        message_id="msg-2",
        peer_id="peer",
        sender_id="sender",
        text=f"批准 {approval.code}",
        source="test",
        session_alias_prefix="test",
    )
    result = await router.route(message)
    assert result == "planner handled approval text"
    assert len(received_intents) == 1
    assert received_intents[0].text == f"批准 {approval.code}"


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
