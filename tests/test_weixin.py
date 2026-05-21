from __future__ import annotations

import pytest

from navi.provider import ChatMessage, MockProvider, ModelPool
from navi.runtime import AgentRuntime
from navi.weixin.client import WeixinClient
from navi.weixin.config import WeixinConfig
from navi.weixin.models import WeixinAccount, WeixinUpdate
from navi.weixin.service import WeixinService
from navi.weixin.store import ContextTokenStore, MessageDeduplicator, WeixinStore, extract_text, split_text_for_weixin


class ScriptedProvider(MockProvider):
    def __init__(self, response: str | list[str]):
        self.response = response
        self.messages: list[ChatMessage] = []

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.messages = messages
        if isinstance(self.response, list):
            return self.response.pop(0)
        return self.response


class PlannerThenMockProvider(MockProvider):
    def __init__(self, decision: str = '{"tool":"final.answer","confidence":1.0,"reason":"ordinary chat"}'):
        self.decision = decision
        self.messages: list[ChatMessage] = []

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.messages = messages
        if messages and "model syscall planner" in messages[0].content:
            return self.decision
        return await super().complete(messages)


def _pool(provider=None) -> ModelPool:
    return ModelPool(default=provider or MockProvider())


@pytest.mark.asyncio
async def test_weixin_handle_update_replies_and_saves_context(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    runtime = AgentRuntime(home=tmp_path, provider=_pool(PlannerThenMockProvider()))
    service = WeixinService(home=tmp_path, config=WeixinConfig(dm_policy="open"), runtime=runtime)
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
    session_id = runtime.memory.current_session_id("connector:weixin:peer")
    assert runtime.memory.get_messages(session_id)


@pytest.mark.asyncio
async def test_weixin_session_control_text_routes_as_message(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    runtime = AgentRuntime(home=tmp_path, provider=_pool(PlannerThenMockProvider()))
    service = WeixinService(home=tmp_path, config=WeixinConfig(dm_policy="open"), runtime=runtime)
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")

    await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-1", peer_id="peer", sender_id="sender", text="first"),
    )
    first_session = runtime.memory.current_session_id("connector:weixin:peer")

    handled = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-2", peer_id="peer", sender_id="sender", text="开启一个新的会话"),
    )
    second_session = runtime.memory.current_session_id("connector:weixin:peer")

    assert handled is True
    assert first_session == second_session
    assert service.client.sent[-1]["text"] == "Navi received: 开启一个新的会话"


@pytest.mark.asyncio
async def test_weixin_session_current_text_routes_as_message(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    runtime = AgentRuntime(home=tmp_path, provider=_pool(PlannerThenMockProvider()))
    service = WeixinService(home=tmp_path, config=WeixinConfig(dm_policy="open"), runtime=runtime)
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")

    handled = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-session-current", peer_id="peer", sender_id="sender", text="当前会话是什么"),
    )

    assert handled is True
    assert service.client.sent[-1]["text"] == "Navi received: 当前会话是什么"


@pytest.mark.asyncio
async def test_weixin_plain_schedule_message_creates_watch(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    runtime = AgentRuntime(
        home=tmp_path,
        provider=_pool(ScriptedProvider(
            [
                '{"tool":"watch.create","permission":"prepare","args":{"prompt":"进行毛选晨读","cron":"0 8 * * *"},"confidence":0.95,"reason":"explicit recurring schedule"}',
                '{"tool":"final.answer","permission":"read","args":{"message":"已为你创建每天早上 8 点的晨读提醒。"},"confidence":0.95,"reason":"watch created"}',
            ]
        )),
    )
    service = WeixinService(home=tmp_path, config=WeixinConfig(dm_policy="open"), runtime=runtime)
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")

    handled = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-watch", peer_id="peer", sender_id="sender", text="每天早上 8 点进行毛选晨读"),
    )

    assert handled is True
    watches = service.active.tasks.list_watches()
    assert watches[0].cron == "0 8 * * *"
    assert watches[0].prompt == "进行毛选晨读"
    assert "晨读提醒" in service.client.sent[-1]["text"]


@pytest.mark.asyncio
async def test_weixin_plain_schedule_with_vague_period_asks_clarification(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    runtime = AgentRuntime(
        home=tmp_path,
        provider=_pool(ScriptedProvider(
            '{"tool":"clarify.ask","args":{"message":"你希望每天晚上几点上通识课？"},"confidence":0.91,"reason":"vague time"}'
        )),
    )
    service = WeixinService(home=tmp_path, config=WeixinConfig(dm_policy="open"), runtime=runtime)
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")

    handled = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-watch-period", peer_id="peer", sender_id="sender", text="每天晚上上一个通识课给我"),
    )

    assert handled is True
    assert service.active.tasks.list_watches() == []
    assert service.client.sent[-1]["text"] == "你希望每天晚上几点上通识课？"
    assert "watch.create" not in service.client.sent[-1]["text"]


@pytest.mark.asyncio
async def test_weixin_plain_local_action_creates_task(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    runtime = AgentRuntime(
        home=tmp_path,
        provider=_pool(ScriptedProvider(
            [
                '{"tool":"task.create","permission":"prepare","args":{"prompt":"列一下我本机的目录"},"confidence":0.9,"reason":"local filesystem request"}',
                '{"tool":"final.answer","permission":"read","args":{"message":"已创建列目录任务，等待审批。"},"confidence":0.95,"reason":"task prepared"}',
            ]
        )),
    )
    service = WeixinService(home=tmp_path, config=WeixinConfig(dm_policy="open"), runtime=runtime)
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")

    handled = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-task", peer_id="peer", sender_id="sender", text="列一下我本机的目录"),
    )

    assert handled is True
    task = service.active.tasks.list()[0]
    assert task.status == "awaiting_approval"
    assert task.prompt == "列一下我本机的目录"
    assert "等待审批" in service.client.sent[-1]["text"]
    assert "approval.resolve" not in service.client.sent[-1]["text"]


@pytest.mark.asyncio
async def test_weixin_command_like_business_text_routes_through_planner(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    runtime = AgentRuntime(
        home=tmp_path,
        provider=_pool(ScriptedProvider(
            [
                '{"tool":"task.create","permission":"prepare","args":{"prompt":"列一下我本机的目录"},"confidence":0.9,"reason":"local filesystem request"}',
                '{"tool":"final.answer","permission":"read","args":{"message":"已创建列目录任务，等待审批。"},"confidence":0.95,"reason":"task prepared"}',
            ]
        )),
    )
    service = WeixinService(home=tmp_path, config=WeixinConfig(dm_policy="open"), runtime=runtime)
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")

    handled = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-task-plain", peer_id="peer", sender_id="sender", text="创建任务：列一下我本机的目录"),
    )

    assert handled is True
    task = service.active.tasks.list()[0]
    assert task.status == "awaiting_approval"
    assert task.prompt == "列一下我本机的目录"


@pytest.mark.asyncio
async def test_weixin_plain_approval_queues_task(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    provider = ScriptedProvider(
        [
            '{"tool":"task.create","permission":"prepare","args":{"prompt":"列一下我本机的目录"},"confidence":0.9,"reason":"local filesystem request"}',
            '{"tool":"final.answer","permission":"read","args":{"message":"已创建列目录任务，等待审批。"},"confidence":0.95,"reason":"task prepared"}',
        ]
    )
    runtime = AgentRuntime(home=tmp_path, provider=_pool(provider))
    service = WeixinService(home=tmp_path, config=WeixinConfig(dm_policy="open"), runtime=runtime)
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")

    await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-task", peer_id="peer", sender_id="sender", text="列一下我本机的目录"),
    )
    task = service.active.tasks.list()[0]
    code = service.active.tasks.list_approvals()[0].code
    provider.response = [
        (
            '{"tool":"approval.resolve","permission":"write","args":{"decision":"approve","code":"'
            + code
            + '"},"confidence":0.95,"reason":"explicit approval"}'
        ),
        '{"tool":"final.answer","permission":"read","args":{"message":"任务已批准并加入执行队列。"},"confidence":0.95,"reason":"approval resolved"}',
    ]

    handled = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-approval", peer_id="peer", sender_id="sender", text=f"批准 {code}"),
    )

    assert handled is True
    assert service.active.tasks.get(task.id).status == "queued"
    assert "执行队列" in service.client.sent[-1]["text"]


@pytest.mark.asyncio
async def test_weixin_plain_task_status_uses_fact_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    provider = ScriptedProvider(
        '{"tool":"task.status","permission":"read","args":{},"confidence":0.9,"reason":"task status request"}'
    )
    runtime = AgentRuntime(
        home=tmp_path,
        provider=_pool(provider),
    )
    service = WeixinService(home=tmp_path, config=WeixinConfig(dm_policy="open"), runtime=runtime)
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")
    task = service.active.tasks.create("check startup", status="preparing")
    provider.response = [
        (
            '{"tool":"task.status","permission":"read","args":{"task_id":"'
            + task.id
            + '"},"confidence":0.9,"reason":"task status request"}'
        ),
        '{"tool":"final.answer","permission":"read","args":{"message":"任务 '
        + task.id
        + ' 当前状态是 preparing。"},"confidence":0.95,"reason":"facts observed"}',
    ]

    handled = await service.handle_update(
        account,
        WeixinUpdate(
            message_id="msg-task-status",
            peer_id="peer",
            sender_id="sender",
            text=f"{task.id} 为什么没有执行",
        ),
    )

    assert handled is True
    assert task.id in service.client.sent[-1]["text"]
    assert "preparing" in service.client.sent[-1]["text"]


@pytest.mark.asyncio
async def test_weixin_plain_service_status_uses_fact_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    runtime = AgentRuntime(
        home=tmp_path,
        provider=_pool(ScriptedProvider(
            [
                '{"tool":"service.status","permission":"read","args":{"name":"navi.service"},"confidence":0.9,"reason":"service status request"}',
                '{"tool":"final.answer","permission":"read","args":{"message":"navi.service 的启动时间是 Tue 2026-05-19 08:00:00 CST。"},"confidence":0.95,"reason":"facts observed"}',
            ]
        )),
    )
    service = WeixinService(home=tmp_path, config=WeixinConfig(dm_policy="open"), runtime=runtime)
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")

    import navi.core_tools as tools_module
    from navi.fact_tools import ServiceFacts

    monkeypatch.setattr(
        tools_module,
        "service_facts",
        lambda name: ServiceFacts(
            name=name,
            properties={
                "ActiveState": "active",
                "SubState": "running",
                "MainPID": "123",
                "ActiveEnterTimestamp": "Tue 2026-05-19 08:00:00 CST",
            },
            exit_code=0,
            stderr="",
        ),
    )

    handled = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-service", peer_id="peer", sender_id="sender", text="你的最新启动时间是什么时候"),
    )

    assert handled is True
    assert "Tue 2026-05-19 08:00:00 CST" in service.client.sent[-1]["text"]


@pytest.mark.asyncio
async def test_weixin_dm_allowlist_blocks_untrusted_sender(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    runtime = AgentRuntime(home=tmp_path, provider=_pool())
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


@pytest.mark.asyncio
async def test_weixin_pairing_policy_blocks_unlisted_sender(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    runtime = AgentRuntime(home=tmp_path, provider=_pool())
    service = WeixinService(home=tmp_path, config=WeixinConfig(dm_policy="pairing"), runtime=runtime)
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")

    handled = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-default-policy", peer_id="peer", sender_id="sender", text="ping"),
    )

    assert handled is False
    assert service.client.sent == []


@pytest.mark.asyncio
async def test_weixin_planner_parse_failure_returns_os_error(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    runtime = AgentRuntime(home=tmp_path, provider=_pool(ScriptedProvider("not-json")))
    service = WeixinService(home=tmp_path, config=WeixinConfig(dm_policy="open"), runtime=runtime)
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")

    handled = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-planner-fail", peer_id="peer", sender_id="sender", text="列一下任务"),
    )

    assert handled is True
    assert "system.planner_error" in service.client.sent[-1]["text"]
    assert runtime.memory.list_sessions()


def test_weixin_prompt_affordances_do_not_expose_connector_context():
    assert not hasattr(WeixinService, "_prompt_context")


@pytest.mark.asyncio
async def test_weixin_transport_details_do_not_enter_model_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    planner_provider = PlannerThenMockProvider()
    runtime = AgentRuntime(home=tmp_path, provider=_pool(planner_provider))
    service = WeixinService(home=tmp_path, config=WeixinConfig(dm_policy="open"), runtime=runtime)
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")

    handled = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-transport-clean", peer_id="peer", sender_id="sender", text="你好"),
    )

    assert handled is True
    prompt = "\n".join(message.content for message in planner_provider.messages)
    assert "Weixin" not in prompt
    assert "connector.weixin" not in prompt
    assert "transport channel" not in prompt
    assert "Surface:" not in prompt


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
    runtime = AgentRuntime(home=tmp_path, provider=_pool())
    service = WeixinService(home=tmp_path, config=WeixinConfig(dm_policy="open"), runtime=runtime)
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


@pytest.mark.asyncio
async def test_weixin_background_task_notification_uses_model_text(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    provider = ScriptedProvider("任务完成：目录已经列出。")
    runtime = AgentRuntime(home=tmp_path, provider=_pool(provider))
    service = WeixinService(home=tmp_path, config=WeixinConfig(dm_policy="open"), runtime=runtime)
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")
    created = service.active.tasks.create(
        "list dir",
        status="completed",
        peer_id="peer",
    )
    task = service.active.tasks.update_task(created.id, result_summary="listed files")
    assert task is not None

    async def no_watches():
        return []

    async def completed_tasks():
        return [task]

    monkeypatch.setattr(service.active, "process_watches_once", no_watches)
    monkeypatch.setattr(service.active, "process_queue_once", completed_tasks)

    await service.process_background(account)

    assert service.client.sent[-1]["text"] == "任务完成：目录已经列出。"
    assert "task_execution_finished" in provider.messages[-1].content


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
