from __future__ import annotations

import os

import pytest

from navi.config import WeixinConfig
from navi.provider import MockProvider
from navi.runtime import AgentRuntime
from navi.weixin.client import WeixinClient
from navi.weixin.models import WeixinAccount, WeixinUpdate
from navi.weixin.service import WeixinService
from navi.weixin.store import ContextTokenStore, MessageDeduplicator, WeixinStore, extract_text, split_text_for_weixin


@pytest.mark.asyncio
async def test_weixin_handle_update_replies_and_saves_context(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    runtime = AgentRuntime(home=tmp_path, provider=MockProvider())
    service = WeixinService(home=tmp_path, config=WeixinConfig(), runtime=runtime)
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")

    handled = await service.handle_update(
        account,
        WeixinUpdate(
            message_id="msg-1",
            peer_id="peer",
            sender_id="sender",
            text="ping",
            context_token="ctx",
        ),
    )

    assert handled is True
    assert service.context_tokens.get("acct", "peer") == "ctx"
    assert service.client.sent[0]["text"] == "Navi received: ping"
    assert runtime.memory.get_messages("weixin:peer")


@pytest.mark.asyncio
async def test_weixin_dm_allowlist_blocks_untrusted_sender(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    runtime = AgentRuntime(home=tmp_path, provider=MockProvider())
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(dm_policy="allowlist", allowed_users=["trusted"]),
        runtime=runtime,
    )
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")

    handled = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-2", peer_id="peer", sender_id="unknown", text="ping"),
    )

    assert handled is False
    assert service.client.sent == []


def test_context_token_store_persists(tmp_path):
    store = ContextTokenStore(tmp_path)
    store.put("acct", "peer", "ctx")

    reloaded = ContextTokenStore(tmp_path)

    assert reloaded.get("acct", "peer") == "ctx"


def test_message_deduplicator_blocks_repeats():
    dedup = MessageDeduplicator()

    assert dedup.seen("same") is False
    assert dedup.seen("same") is True


@pytest.mark.asyncio
async def test_weixin_content_dedup_blocks_repeated_text_with_new_message_id(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    runtime = AgentRuntime(home=tmp_path, provider=MockProvider())
    service = WeixinService(home=tmp_path, config=WeixinConfig(), runtime=runtime)
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")

    first = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-a", peer_id="peer", sender_id="sender", text="same"),
    )
    second = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-b", peer_id="peer", sender_id="sender", text="same"),
    )

    assert first is True
    assert second is False
    assert len(service.client.sent) == 1


def test_weixin_extract_text_from_ilink_item_list():
    payload = {
        "item_list": [
            {
                "type": 1,
                "text_item": {"text": "hello"},
                "ref_msg": {
                    "title": "quoted",
                    "message_item": {"type": 1, "text_item": {"text": "previous"}},
                },
            }
        ]
    }

    assert extract_text(payload) == "[引用: quoted | previous]\nhello"


def test_weixin_extract_voice_transcript():
    payload = {"item_list": [{"type": 3, "voice_item": {"text": "transcribed voice"}}]}

    assert extract_text(payload) == "transcribed voice"


def test_weixin_split_text_filters_empty_and_splits_chatty_lines():
    assert split_text_for_weixin("") == []
    assert split_text_for_weixin("first\nsecond\nthird") == ["first", "second", "third"]


def test_weixin_sync_buf_persists(tmp_path):
    store = WeixinStore(tmp_path)
    store.save_sync_buf("acct", "sync-token")

    assert WeixinStore(tmp_path).load_sync_buf("acct") == "sync-token"


@pytest.mark.asyncio
async def test_mock_weixin_client_splits_long_messages(monkeypatch):
    from navi.weixin.client import MockWeixinClient

    client = MockWeixinClient()
    long_text = "a" * 2100

    await client.send_message(account_id="acct", peer_id="peer", text=long_text, context_token="ctx")

    assert len(client.sent) == 2
    assert all(item["context_token"] == "ctx" for item in client.sent)


@pytest.mark.asyncio
async def test_weixin_send_retries_without_context_on_session_expiry():
    class RecordingClient(WeixinClient):
        def __init__(self):
            super().__init__(base_url="https://weixin.example.com", token="token")
            self.payloads = []

        async def _post(self, path, payload, *, timeout):
            self.payloads.append(payload)
            if len(self.payloads) == 1:
                return {"errcode": -14, "errmsg": "session expired"}
            return {"errcode": 0}

        async def _sleep(self, seconds):
            return None

    client = RecordingClient()

    await client.send_message(account_id="acct", peer_id="peer", text="hello", context_token="ctx")

    assert client.payloads[0]["msg"]["context_token"] == "ctx"
    assert "context_token" not in client.payloads[1]["msg"]


@pytest.mark.asyncio
async def test_weixin_send_retries_rate_limit():
    class RecordingClient(WeixinClient):
        def __init__(self):
            super().__init__(base_url="https://weixin.example.com", token="token")
            self.calls = 0

        async def _post(self, path, payload, *, timeout):
            self.calls += 1
            if self.calls == 1:
                return {"errcode": -2, "errmsg": "freq limit"}
            return {"errcode": 0}

        async def _sleep(self, seconds):
            return None

    client = RecordingClient()

    await client.send_message(account_id="acct", peer_id="peer", text="hello", context_token="")

    assert client.calls == 2
