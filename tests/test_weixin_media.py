from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.runtime import AgentRuntime
from navi.runs import Run
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
    )

    send_payload = calls[-1][1]["msg"]
    assert send_payload["to_user_id"] == "wx-user"
    assert send_payload["context_token"] == "ctx"
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


class StaticIngress:
    def __init__(self, text: str) -> None:
        self.text = text

    async def handle(self, message: Any) -> str:
        return self.text


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
async def test_service_sends_media_directive_from_weixin_outbox(tmp_path: Path):
    outbox = tmp_path / "weixin" / "outbox"
    outbox.mkdir(parents=True)
    report = outbox / "report.pdf"
    report.write_bytes(b"report")
    client = CaptureWeixinClient()
    service = WeixinService(
        home=tmp_path,
        config=WeixinConfig(),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )
    service.ingress = StaticIngress(f"MEDIA:{report}\n已生成报告。")

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
    assert client.messages[0]["text"] == "已生成报告。"


@pytest.mark.asyncio
async def test_weixin_stage_file_returns_allowed_media_directive(tmp_path: Path):
    source = tmp_path / "resume.docx"
    source.write_bytes(b"resume")
    registry = build_capability_registry(tmp_path, project_dir=tmp_path, governed_run_id="test-run")

    result = await registry.invoke(
        "connector.weixin.stage_file",
        {"path": str(source)},
        permission="write",
        context=CapabilityContext(home=tmp_path, source="local", workspace=str(tmp_path)),
    )

    assert result.ok is True
    assert result.facts is not None
    staged = Path(result.facts["outbound_path"])
    assert staged.is_file()
    assert staged.read_bytes() == b"resume"
    assert staged.is_relative_to((tmp_path / "weixin" / "outbox").resolve())
    assert result.facts["media_directive"] == f"MEDIA:{staged}"


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
                status="completed",
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
