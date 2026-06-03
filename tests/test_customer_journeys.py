from __future__ import annotations

import json
import time

import pytest

from navi.execution import EXECUTION_PROTOCOL_VERSION, NaviExecutionProvider
from navi.provider import ChatMessage, ModelPool
from navi.runtime import AgentRuntime
from navi.weixin.config import WeixinConfig
from navi.weixin.models import WeixinAccount, WeixinUpdate
from navi.weixin.service import WeixinService


class JourneyProvider:
    def __init__(self, responses: list[str]):
        self.responses = responses
        self.messages: list[list[ChatMessage]] = []

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.messages.append(messages)
        return self.responses.pop(0)


def _service(tmp_path, provider: JourneyProvider) -> tuple[WeixinService, WeixinAccount]:
    runtime = AgentRuntime(home=tmp_path, provider=ModelPool(default=provider))
    service = WeixinService(home=tmp_path, config=WeixinConfig(dm_policy="open"), runtime=runtime)
    account = WeixinAccount(account_id="acct", token="token", base_url="mock://ilink")
    return service, account


def _watch_protocol(message: str) -> str:
    return json.dumps(
        {
            "navi_execution": {
                "version": EXECUTION_PROTOCOL_VERSION,
                "phase": "watch",
                "run_id": "",
                "plan_id": "customer-watch",
                "steps": [
                    {
                        "id": "notify",
                        "actions": [
                            {
                                "tool": "final.answer",
                                "permission": "read",
                                "args": {"message": message},
                            }
                        ],
                        "verification": {"checks": [], "reason": "scheduled customer notification"},
                        "on_failure": "stop",
                    }
                ],
                "evidence": [],
                "verification": {"status": "proposed", "checks": [], "reason": "model proposal"},
                "completion": {"status": "proposed", "summary": message},
            }
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_customer_journey_lists_then_cleans_failed_watch_records(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    provider = JourneyProvider(
        [
            '{"tool":"delegate.list","permission":"read","args":{},"confidence":0.95,"reason":"customer asks current tasks"}',
            '{"tool":"final.answer","permission":"read","args":{"message":"当前有 3 条 failed watch task 记录，可以清理。"},"confidence":0.95,"reason":"summarize task facts"}',
            '{"tool":"delegate.delete","permission":"write","args":{"status":"failed","source":"watch","kind":"delegation"},"confidence":0.95,"reason":"customer confirmed cleanup"}',
            '{"tool":"final.answer","permission":"read","args":{"message":"已清理 3 条 failed watch task 记录，剩余 0 条。"},"confidence":0.95,"reason":"cleanup verified"}',
        ]
    )
    service, account = _service(tmp_path, provider)
    for index in range(3):
        service.active.runs.create(f"stale watch {index}", status="failed", source="watch", kind="delegation", workspace=str(tmp_path))

    listed = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-list", peer_id="peer", sender_id="sender", text="我们现在都有哪些任务"),
    )
    cleaned = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-clean", peer_id="peer", sender_id="sender", text="清理"),
    )

    assert listed is True
    assert cleaned is True
    assert service.active.runs.count_runs(status="failed", source="watch", kind="delegation") == 0
    assert service.client.sent[-1]["text"] == "已清理 3 条 failed watch task 记录，剩余 0 条。"
    tool_logs = service.active.runs.list_tool_call_logs(limit=10)
    assert [log.tool for log in tool_logs[:2]] == ["delegate.delete", "delegate.list"]
    assert provider.responses == []


@pytest.mark.asyncio
async def test_customer_journey_scheduled_watch_sends_message_without_failed_task_residue(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")
    provider = JourneyProvider(
        [
            '{"tool":"watch.create","permission":"prepare","args":{"prompt":"讲解通识知识","cron":"0 20 * * *"},"confidence":0.95,"reason":"customer scheduled watch"}',
            '{"tool":"final.answer","permission":"read","args":{"message":"已创建每天晚上 8 点的通识知识定时任务。"},"confidence":0.95,"reason":"watch created"}',
            _watch_protocol("今晚的通识知识：证据由 Navi actuator 生成。"),
        ]
    )
    service, account = _service(tmp_path, provider)
    service.daemon.execution.provider = NaviExecutionProvider(provider=ModelPool(default=provider), timeout_seconds=5)

    created = await service.handle_update(
        account,
        WeixinUpdate(message_id="msg-watch-create", peer_id="peer", sender_id="sender", text="每天晚上8点讲解通识知识"),
    )
    watch = service.active.runs.list_watches()[0]
    service.active.runs.mark_watch_run(watch.id, last_run_at=0, next_run_at=time.time() - 1)

    await service.process_background(account)

    assert created is True
    assert service.client.sent[0]["text"] == "已创建每天晚上 8 点的通识知识定时任务。"
    assert service.client.sent[-1]["text"] == "今晚的通识知识：证据由 Navi actuator 生成。"
    assert service.active.runs.count_runs(status="failed", source="watch", kind="delegation") == 0
    phases = {log.phase for log in service.active.runs.list_execution_logs(limit=10)}
    assert {"watch", "watch_protocol"} <= phases
    protocol_log = next(log for log in service.active.runs.list_execution_logs(limit=10) if log.phase == "watch_protocol")
    recorded = json.loads(protocol_log.stdout)
    assert recorded["evidence"]
    assert recorded["verification"]["status"] == "completed"
    assert provider.responses == []
