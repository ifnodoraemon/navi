from __future__ import annotations

from typing import Any

import pytest

from navi.connector_runtime import (
    ConnectorIngressDeduplicator,
    ConnectorMessage,
)
from navi.runtime import AgentRuntime
from navi.telegram.config import TelegramConfig
from navi.telegram.models import TelegramUpdate
from navi.telegram.service import TelegramService


class NoModelCalls:
    async def complete_for(self, role: str, messages: list[Any], **kwargs: Any) -> str:
        raise AssertionError(f"unexpected model call: {role}")

    def list_roles(self) -> list[str]:
        return []


class StaticIngress:
    def __init__(self, text: str) -> None:
        self.text = text

    async def handle(self, message: Any) -> str:
        return self.text


class CaptureTelegramClient:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    async def send_message(self, *, chat_id: str, text: str) -> None:
        self.messages.append({"chat_id": chat_id, "text": text})


def _message(message_id: str, *, text: str = "hello") -> ConnectorMessage:
    return ConnectorMessage(
        message_id=message_id,
        peer_id="peer-1",
        sender_id="sender-1",
        text=text,
        source="test-connector",
        session_alias_prefix="connector:test",
    )


def _peer_message(message_id: str, peer_id: str, *, text: str = "hello") -> ConnectorMessage:
    return ConnectorMessage(
        message_id=message_id,
        peer_id=peer_id,
        sender_id="sender-1",
        text=text,
        source="test-connector",
        session_alias_prefix="connector:test",
    )


def test_connector_ingress_deduplicates_message_id_across_instances(tmp_path):
    first = ConnectorIngressDeduplicator(tmp_path)
    second = ConnectorIngressDeduplicator(tmp_path)

    assert first.check(_message("msg-1")).duplicate is False
    duplicate = second.check(_message("msg-1", text="changed text"))

    assert duplicate.duplicate is True
    assert duplicate.reason == "message_id"


def test_connector_session_alias_is_actor_scoped() -> None:
    assert _message("msg-1").session_alias == "connector:test:peer-1:sender-1"


def test_connector_ingress_scopes_message_id_and_content_to_peer(tmp_path):
    dedup = ConnectorIngressDeduplicator(tmp_path)

    assert dedup.check(_peer_message("msg-1", "peer-1", text="same text")).duplicate is False
    assert dedup.check(_peer_message("msg-1", "peer-2", text="same text")).duplicate is False


def test_connector_ingress_deduplicates_content_across_message_ids(tmp_path):
    dedup = ConnectorIngressDeduplicator(tmp_path)

    assert dedup.check(_message("msg-1", text="same text")).duplicate is False
    duplicate = dedup.check(_message("msg-2", text="same text"))

    assert duplicate.duplicate is True
    assert duplicate.reason == "content"


@pytest.mark.asyncio
async def test_telegram_service_uses_shared_persistent_ingress_dedup(tmp_path):
    update = TelegramUpdate(
        update_id=1,
        message_id=10,
        chat_id="chat-1",
        sender_id="sender-1",
        text="hello",
    )

    first_client = CaptureTelegramClient()
    first = TelegramService(
        home=tmp_path,
        config=TelegramConfig(dm_policy="open"),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=first_client,
    )
    first.ingress = StaticIngress("first response")

    second_client = CaptureTelegramClient()
    second = TelegramService(
        home=tmp_path,
        config=TelegramConfig(dm_policy="open"),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=second_client,
    )
    second.ingress = StaticIngress("second response")

    assert await first.handle_update(update) is True
    assert await second.handle_update(update) is False
    assert first_client.messages == [{"chat_id": "chat-1", "text": "first response"}]
    assert second_client.messages == []
