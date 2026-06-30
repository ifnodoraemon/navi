from __future__ import annotations

import pytest

from navi.connector_router import ConnectorRouter, _parse_connector_approval_command
from navi.connector_runtime import ConnectorMessage
from navi.event_bus import EventBus
from navi.runs import RunStore


def _message(text: str, *, source: str = "weixin") -> ConnectorMessage:
    return ConnectorMessage(
        message_id="msg-approval",
        peer_id="peer",
        sender_id="sender",
        text=text,
        source=source,
        session_alias_prefix="test",
    )


def test_connector_approval_command_parser_uses_connector_spec() -> None:
    assert _parse_connector_approval_command(_message("批准 123456")) == (
        "approve",
        "123456",
    )
    assert _parse_connector_approval_command(_message("approve 123456")) == (
        "approve",
        "123456",
    )
    assert _parse_connector_approval_command(_message("拒绝 123456")) == (
        "reject",
        "123456",
    )
    assert _parse_connector_approval_command(_message("批准 `123456`")) == (
        "approve",
        "123456",
    )
    assert _parse_connector_approval_command(_message("批准最新的")) is None
    assert _parse_connector_approval_command(_message("批准 123456 now")) is None


@pytest.mark.asyncio
async def test_router_resolves_explicit_approval_without_llm_loop(tmp_path) -> None:
    bus = EventBus()
    router = ConnectorRouter(tmp_path, bus)
    ingress_seen = False

    async def on_message_ingress(event) -> None:
        nonlocal ingress_seen
        ingress_seen = True

    bus.subscribe("message_ingress", on_message_ingress)

    runs = RunStore(tmp_path)
    task = runs.create(
        "Needs approval",
        source="weixin",
        peer_id="peer",
        sender_id="sender",
        workspace=str(tmp_path),
        status="awaiting_approval",
    )
    approval = runs.create_approval(
        run_id=task.id,
        peer_id="peer",
        sender_id="sender",
    )

    result = await router.route(_message(f"批准 {approval.code}"))

    assert ingress_seen is False
    assert "approval_request status=approved" in result
    assert runs.get(task.id).status == "queued"
    await bus.shutdown()
