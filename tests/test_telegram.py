from __future__ import annotations

import pytest

from navi.provider import MockProvider, ModelPool
from navi.runtime import AgentRuntime
from navi.telegram.config import TelegramConfig
from navi.telegram.models import TelegramUpdate
from navi.telegram.service import TelegramService


@pytest.mark.asyncio
async def test_telegram_handle_update_replies_and_saves_context(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_TELEGRAM_MOCK", "true")
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=MockProvider()))
    service = TelegramService(
        home=tmp_path,
        config=TelegramConfig(dm_policy="open"),
        runtime=runtime,
        project_dir=tmp_path,
    )

    handled = await service.handle_update(
        TelegramUpdate(
            update_id=1,
            message_id=10,
            chat_id="chat",
            sender_id="sender",
            text="hello",
        )
    )

    assert handled is True
    assert service.client.sent[-1]["chat_id"] == "chat"
    assert service.client.sent[-1]["text"] == "Navi received: hello"
    session_id = runtime.memory.current_session_id("connector:telegram:chat")
    assert runtime.memory.get_messages(session_id)[0].content == "hello"


@pytest.mark.asyncio
async def test_telegram_allowlist_blocks_untrusted_sender(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_TELEGRAM_MOCK", "true")
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=MockProvider()))
    service = TelegramService(
        home=tmp_path,
        config=TelegramConfig(dm_policy="allowlist", allowed_users=["trusted"]),
        runtime=runtime,
        project_dir=tmp_path,
    )

    handled = await service.handle_update(
        TelegramUpdate(
            update_id=1,
            message_id=10,
            chat_id="chat",
            sender_id="sender",
            text="hello",
        )
    )

    assert handled is False
    assert service.client.sent == []
