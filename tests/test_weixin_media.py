from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from navi.approval_contract import APPROVAL_ACTION_CAPABILITY, APPROVAL_DECISION_APPROVE
from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.connector_delivery import ConnectorDelivery, connector_delivery_client_id
from navi.event_bus import ResponseReadyEvent
from navi.goals import GoalStore
from navi.lifecycle import Acceptance, Governance, Phase, Resolution
from navi.runtime import AgentRuntime
from navi.runs import Run, RunStore
from navi.trace import TraceStore
from navi.weixin.client import ITEM_FILE, MEDIA_FILE, WeixinClient
from navi.weixin.config import WeixinConfig
from navi.weixin.models import WeixinAccount, WeixinUpdate
from navi.weixin.service import WeixinService


class NoModelCalls:
    async def complete_for(self, role: str, messages: list[Any], **kwargs: Any) -> str:
        raise AssertionError(f"unexpected model call: {role}")

    def list_roles(self) -> list[str]:
        return []


@pytest.mark.asyncio
async def test_get_updates_keeps_file_only_messages_as_attachment(monkeypatch: pytest.MonkeyPatch):
    client = WeixinClient(base_url="https://ilink.example", token="token")

    async def fake_post(path: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        assert path == "/ilink/bot/getupdates"
        return {
            "msgs": [
                {
                    "message_id": "msg-file",
                    "from_user_id": "wx-user",
                    "item_list": [
                        {
                            "type": ITEM_FILE,
                            "file_item": {
                                "file_name": "report.pdf",
                                "len": "42",
                                "media": {"encrypt_query_param": "encrypted-media"},
                            },
                        }
                    ],
                }
            ],
            "get_updates_buf": "next",
        }

    monkeypatch.setattr(client, "_post", fake_post)

    batch = await client.get_updates("acct", sync_buf="prev")

    assert batch.sync_buf == "next"
    assert len(batch.updates) == 1
    update = batch.updates[0]
    assert update.text == ""
    assert update.attachments[0].kind == "file"
    assert update.attachments[0].file_name == "report.pdf"
    assert update.attachments[0].media_id == "encrypted-media"


@pytest.mark.asyncio
async def test_send_file_uploads_cdn_media_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "report.pdf"
    target.write_bytes(b"%PDF-1.4 test")
    client = WeixinClient(
        base_url="https://ilink.example",
        cdn_base_url="https://cdn.example/c2c",
        token="token",
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_post(path: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        del timeout
        calls.append((path, payload))
        if path == "/ilink/bot/getuploadurl":
            assert payload["media_type"] == MEDIA_FILE
            assert payload["to_user_id"] == "wx-user"
            assert payload["rawsize"] == target.stat().st_size
            return {"upload_param": "upload-param"}
        if path == "/ilink/bot/sendmessage":
            return {"ret": 0, "errcode": 0}
        raise AssertionError(f"unexpected path: {path}")

    async def fake_upload_ciphertext(*, upload_url: str, ciphertext: bytes) -> str:
        assert upload_url.startswith("https://cdn.example/c2c/upload?")
        assert len(ciphertext) >= target.stat().st_size
        return "encrypted-query"

    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(client, "_upload_ciphertext", fake_upload_ciphertext)

    await client.send_file(
        account_id="acct",
        peer_id="wx-user",
        file_path=target,
        context_token="ctx",
        idempotency_key="delivery-1",
    )

    send_payload = calls[-1][1]["msg"]
    assert send_payload["to_user_id"] == "wx-user"
    assert send_payload["context_token"] == "ctx"
    assert send_payload["client_id"] == connector_delivery_client_id(
        "delivery-1", prefix="navi-weixin"
    )
    item = send_payload["item_list"][0]
    assert item["type"] == ITEM_FILE
    assert item["file_item"]["file_name"] == "report.pdf"
    assert item["file_item"]["len"] == str(target.stat().st_size)
    assert item["file_item"]["media"]["encrypt_query_param"] == "encrypted-query"


class CaptureWeixinClient:
    def __init__(self) -> None:
        self.files: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    async def get_typing_ticket(self, *, user_id: str, context_token: str = "") -> str:
        return ""

    async def send_file(self, **kwargs: Any) -> None:
        self.files.append(kwargs)

    async def send_message(self, **kwargs: Any) -> None:
        self.messages.append(kwargs)


class FailingFileWeixinClient(CaptureWeixinClient):
    async def send_file(self, **kwargs: Any) -> None:
        self.files.append(kwargs)
        raise RuntimeError("upload failed")


class StaticIngress:
    def __init__(
        self,
        text: str,
        *,
        action: str = "chat",
        facts: dict[str, Any] | None = None,
    ) -> None:
        self.text = text
        self.action = action
        self.facts = facts or {}

    async def handle(self, message: Any) -> "ResponseReadyEvent":
        return ResponseReadyEvent(
            text=self.text,
            source="weixin",
            action=self.action,
            facts=self.facts,
        )


class StaticDaemon:
    def __init__(self, tasks: list[Run], watch_results: list[dict[str, Any]] | None = None) -> None:
        self._tasks = tasks
        self._watch_results = list(watch_results or [])
        self.runs = {task.id: task for task in tasks}

    async def process_watches_once(self) -> list[dict[str, Any]]:
        return self._watch_results

    async def process_queue_once(self) -> list[Run]:
        return self._tasks


@pytest.mark.asyncio
async def test_service_executes_structured_delivery_from_original_path(tmp_path: Path):
    report = tmp_path / "report.pdf"
    report.write_bytes(b"report")
    client = CaptureWeixinClient()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )
    contract = ConnectorDelivery(
        path=str(report.resolve()),
        text="已生成报告。",
        delivery_id="delivery-report",
    )
    service.ingress = StaticIngress(
        contract.text,
        action="connector_outbound",
        facts={"connector_delivery": contract.to_dict()},
    )

    handled = await service.handle_update(
        WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example"),
        WeixinUpdate(
            message_id="msg-1",
            peer_id="wx-user",
            sender_id="wx-user",
            text="发报告",
            context_token="ctx",
        ),
    )

    assert handled is True
    assert client.files[0]["file_path"] == report.resolve()
    assert client.files[0]["context_token"] == "ctx"
    assert client.files[0]["idempotency_key"] == "delivery-report"
    assert client.messages[0]["text"] == "已生成报告。"
    assert not (tmp_path / "weixin" / "outbox").exists()


@pytest.mark.asyncio
async def test_background_text_does_not_execute_legacy_media_directive(tmp_path: Path):
    resume = tmp_path / "resume.docx"
    resume.write_bytes(b"resume")
    client = CaptureWeixinClient()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )
    service.daemon = StaticDaemon(
        [
            Run(
                id="run-media",
                title="send resume",
                phase=Phase.ENDED,
                governance=Governance.APPROVED,
                acceptance=Acceptance.NONE,
                resolution=Resolution.SUCCESS,
                created_at=1.0,
                updated_at=1.0,
                prompt="send resume",
                source="weixin",
                peer_id="wx-user",
                sender_id="wx-user",
                result_summary=f"MEDIA:{resume}\nHere is your resume file found in the home directory.",
            )
        ]
    )

    await service.process_background(
        WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example")
    )

    assert client.files == []
    assert client.messages[0]["text"] == (
        f"MEDIA:{resume}\nHere is your resume file found in the home directory."
    )
    events = (tmp_path / "weixin" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert not any('"event": "reply.media.sent"' in line for line in events)
    assert any('"event": "background.sent"' in line and '"media_count": 0' in line for line in events)


@pytest.mark.asyncio
async def test_background_send_records_delivery_fact_after_client_success(tmp_path: Path):
    task = Run(
        id="run-delivery",
        title="scheduled lesson",
        phase=Phase.ENDED,
        governance=Governance.NONE,
        acceptance=Acceptance.ACCEPTED,
        resolution=Resolution.SUCCESS,
        created_at=1.0,
        updated_at=1.0,
        prompt="scheduled lesson",
        source="weixin",
        peer_id="wx-user",
        sender_id="wx-user",
        result_summary="Lesson 2: supervised learning.",
    )
    goal = GoalStore(tmp_path).create(
        objective=task.prompt,
        workspace=str(tmp_path),
        source=task.source,
        peer_id=task.peer_id,
        sender_id=task.sender_id,
        run_id=task.id,
    )
    client = CaptureWeixinClient()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )
    service.daemon = StaticDaemon([task])

    await service.process_background(
        WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example")
    )

    delivery = GoalStore(tmp_path).latest_delivery(goal.id)
    assert client.messages[0]["text"] == task.result_summary
    assert delivery["state_transition"] == "delivered"
    assert delivery["channel"] == "weixin"
    assert delivery["text_length"] == len(task.result_summary)
    assert delivery["media_count"] == 0


@pytest.mark.asyncio
async def test_realtime_file_delivery_records_success_only_after_transport(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.xlsx"
    report.write_bytes(b"report")
    run = RunStore(tmp_path).create(
        "deliver report",
        kind="delegation",
        source="weixin",
        peer_id="wx-user",
        sender_id="wx-user",
        workspace=str(tmp_path),
    )
    goal = GoalStore(tmp_path).create(
        objective=run.prompt,
        workspace=str(tmp_path),
        source=run.source,
        peer_id=run.peer_id,
        sender_id=run.sender_id,
        run_id=run.id,
    )
    contract = ConnectorDelivery(
        path=str(report.resolve()),
        text="报告已发送。",
        delivery_id="delivery-success",
        run_id=run.id,
        goal_id=goal.id,
    )
    client = CaptureWeixinClient()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )
    service.ingress = StaticIngress(
        contract.text,
        action="connector_outbound",
        facts={"connector_delivery": contract.to_dict()},
    )

    assert await service.handle_update(
        WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example"),
        WeixinUpdate(
            message_id="msg-delivery-success",
            peer_id="wx-user",
            sender_id="wx-user",
            text="发送报告",
            context_token="ctx",
        ),
    ) is True

    recorded = GoalStore(tmp_path).latest_delivery(goal.id)
    assert recorded["state_transition"] == "delivered"
    assert recorded["media_count"] == 1
    assert recorded["channel"] == "weixin"
    egress = [
        event
        for event in TraceStore(tmp_path).list_events(trace_id="msg-delivery-success")
        if event.phase == "channel.egress"
    ]
    assert len(egress) == 1
    assert egress[0].ok is True


@pytest.mark.asyncio
async def test_realtime_file_delivery_failure_does_not_record_success(
    tmp_path: Path,
) -> None:
    report = tmp_path / "failed-report.xlsx"
    report.write_bytes(b"report")
    run = RunStore(tmp_path).create(
        "deliver failed report",
        kind="delegation",
        source="weixin",
        peer_id="wx-user",
        sender_id="wx-user",
        workspace=str(tmp_path),
    )
    goal = GoalStore(tmp_path).create(
        objective=run.prompt,
        workspace=str(tmp_path),
        source=run.source,
        peer_id=run.peer_id,
        sender_id=run.sender_id,
        run_id=run.id,
    )
    contract = ConnectorDelivery(
        path=str(report.resolve()),
        delivery_id="delivery-failure",
        run_id=run.id,
        goal_id=goal.id,
    )
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=FailingFileWeixinClient(),
    )
    service.ingress = StaticIngress(
        "",
        action="connector_outbound",
        facts={"connector_delivery": contract.to_dict()},
    )

    with pytest.raises(RuntimeError, match="upload failed"):
        await service.handle_update(
            WeixinAccount(
                account_id="acct",
                token="token",
                base_url="https://ilink.example",
            ),
            WeixinUpdate(
                message_id="msg-delivery-failure",
                peer_id="wx-user",
                sender_id="wx-user",
                text="发送报告",
                context_token="ctx",
            ),
        )

    assert GoalStore(tmp_path).latest_delivery(goal.id) == {}
    failure_events = [
        event
        for event in GoalStore(tmp_path).list_events(goal.id)
        if event.event_type == "goal.delivery_failed"
    ]
    assert len(failure_events) == 1
    events = (tmp_path / "weixin" / "events.jsonl").read_text(encoding="utf-8")
    assert '"event": "reply.error"' in events
    assert '"event": "reply.sent"' not in events
    failed_egress = [
        event
        for event in TraceStore(tmp_path).list_events(trace_id="msg-delivery-failure")
        if event.phase == "channel.egress"
    ]
    assert len(failed_egress) == 1
    assert failed_egress[0].ok is False


@pytest.mark.asyncio
async def test_service_deduplicates_message_id_across_instances(tmp_path: Path):
    account = WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example")
    update = WeixinUpdate(
        message_id="msg-repeat",
        peer_id="wx-user",
        sender_id="wx-user",
        text="你好",
        context_token="ctx",
    )

    first_client = CaptureWeixinClient()
    first = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=first_client,
    )
    first.ingress = StaticIngress("第一次回复")

    second_client = CaptureWeixinClient()
    second = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=second_client,
    )
    second.ingress = StaticIngress("第二次不应回复")

    assert await first.handle_update(account, update) is True
    assert await second.handle_update(account, update) is False
    assert first_client.messages[0]["text"] == "第一次回复"
    assert second_client.messages == []

    events = (tmp_path / "weixin" / "events.jsonl").read_text(encoding="utf-8")
    assert '"event": "message.duplicate"' in events


@pytest.mark.asyncio
async def test_send_file_returns_connector_neutral_synchronous_delivery(tmp_path: Path):
    source = tmp_path / "resume.docx"
    source.write_bytes(b"resume")
    args = {"path": str(source)}
    runs = RunStore(tmp_path)
    run = runs.create(
        "deliver file",
        kind="delegation",
        source="local",
        workspace=str(tmp_path),
        phase=Phase.RUNNING,
        governance=Governance.APPROVED,
    )
    approval = runs.create_approval(
        run_id=run.id,
        action=APPROVAL_ACTION_CAPABILITY,
        requested_tool="channel.send_file",
        requested_permission="write",
        args_json=json.dumps(args, ensure_ascii=False, sort_keys=True),
        reason="test approved outbound media delivery",
    )
    runs.resolve_approval(approval.id, decision=APPROVAL_DECISION_APPROVE, resolved_by="test")
    registry = build_capability_registry(tmp_path, project_dir=tmp_path, governed_run_id=run.id)

    result = await registry.invoke(
        "channel.send_file",
        args,
        permission="write",
        context=CapabilityContext(home=tmp_path, source="local", workspace=str(tmp_path)),
    )

    assert result.ok is True
    assert result.terminal is False
    assert result.yields_control is True
    assert result.action == "connector_outbound"
    assert result.facts is not None
    assert "entity_type" in result.facts
    assert result.facts["entity_type"] == "connector_delivery"
    assert result.facts["side_effect_scope"] == "external"
    assert result.facts["side_effect_state"] == "delivery_requested"
    assert result.facts["side_effect_artifact"] == str(source.resolve())
    delivery = result.facts["connector_delivery"]
    assert delivery["kind"] == "file"
    assert delivery["mode"] == "synchronous"
    assert delivery["channel"] == "current"
    assert delivery["path"] == str(source.resolve())
    assert not (tmp_path / "weixin" / "outbox").exists()


@pytest.mark.asyncio
async def test_background_task_without_surface_text_does_not_synthesize_reply(tmp_path: Path):
    client = CaptureWeixinClient()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )
    service.daemon = StaticDaemon(
        [
            Run(
                id="run-empty",
                title="empty completed task",
                phase=Phase.ENDED,
                governance=Governance.NONE,
                acceptance=Acceptance.NONE,
                resolution=Resolution.SUCCESS,
                created_at=1.0,
                updated_at=1.0,
                prompt="do work",
                source="weixin",
                peer_id="wx-user",
                sender_id="wx-user",
            )
        ]
    )

    await service.process_background(
        WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example")
    )

    assert client.messages == []


@pytest.mark.asyncio
async def test_background_watch_without_surface_text_does_not_record_sent_reply(tmp_path: Path):
    client = CaptureWeixinClient()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(home_channel="wx-home"),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )
    service.daemon = StaticDaemon(tasks=[], watch_results=[{"run_id": "watch-empty"}])

    await service.process_background(
        WeixinAccount(account_id="acct", token="token", base_url="https://ilink.example")
    )

    assert client.messages == []
    events = (tmp_path / "weixin" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"event": "background.skipped"' in line for line in events)
    assert not any('"event": "background.sent"' in line for line in events)
