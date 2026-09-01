from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from navi.runtime import AgentRuntime
from navi.telegram.client import TelegramClient
from navi.telegram.config import TelegramConfig
from navi.telegram.models import TelegramAttachment
from navi.telegram.service import TelegramService


class NoModelCalls:
    def list_roles(self) -> list[str]:
        return []


class CaptureIngress:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def handle(self, message: Any) -> Any:
        self.messages.append(message)

        class _Response:
            text = "ok"
            action = "chat"
            facts: dict[str, Any] = {}

        return _Response()


class CaptureTelegramClient:
    def __init__(self, updates: list[Any]) -> None:
        self.updates = updates
        self.sent: list[dict[str, Any]] = []

    async def get_updates(self, *, offset: int | None = None, timeout: int = 25):
        del offset, timeout
        return self.updates

    async def send_message(self, *, chat_id: str, text: str) -> None:
        self.sent.append({"chat_id": chat_id, "text": text})


def _photo_message(update_id: int, caption: str = "") -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": update_id * 10,
        "chat": {"id": 100},
        "from": {"id": 200},
        "photo": [
            {"file_id": "small", "width": 100, "height": 100, "file_size": 1000},
            {"file_id": "large", "width": 800, "height": 600, "file_size": 9000},
        ],
    }
    if caption:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


@pytest.mark.asyncio
async def test_parse_update_keeps_media_only_messages_with_largest_photo():
    from navi.telegram.client import _parse_update

    payload = _photo_message(1)
    update = _parse_update(payload)

    assert update is not None
    assert update.text == ""
    assert len(update.attachments) == 1
    attachment = update.attachments[0]
    assert attachment.kind == "image"
    assert attachment.file_id == "large"
    assert attachment.size == 9000
    assert attachment.file_name == "photo.jpg"


@pytest.mark.asyncio
async def test_get_updates_downloads_document_media(tmp_path: Path, monkeypatch):
    from navi.telegram.client import _parse_update

    client = TelegramClient(
        api_base_url="https://api.example",
        bot_token="token",
        media_dir=tmp_path / "media",
    )

    class _Transport(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request: Any) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/bottoken/getFile"):
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {"file_id": "doc-1", "file_path": "documents/report.pdf"},
                    },
                )
            assert "/file/bottoken/documents/report.pdf" in url
            return httpx.Response(200, content=b"%PDF-1.4 report")

    original_async_client = httpx.AsyncClient

    def fake_client(**kwargs: Any) -> httpx.AsyncClient:
        del kwargs
        return original_async_client(transport=_Transport(), trust_env=False)

    monkeypatch.setattr("navi.telegram.client.httpx.AsyncClient", fake_client)

    parsed = _parse_update(
        {
            "update_id": 7,
            "message": {
                "message_id": 70,
                "chat": {"id": 100},
                "from": {"id": 200},
                "document": {
                    "file_id": "doc-1",
                    "file_name": "report.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 15,
                },
            },
        }
    )
    assert parsed is not None

    update = await client._with_downloaded_attachments(parsed)

    attachment = update.attachments[0]
    assert attachment.download_error == ""
    saved = Path(attachment.local_path)
    assert saved.name == "100-70-0-report.pdf"
    assert saved.read_bytes() == b"%PDF-1.4 report"
    assert attachment.size == 15
    assert not list((tmp_path / "media").glob("*.tmp"))


@pytest.mark.asyncio
async def test_service_synthesizes_media_only_text_and_facts(tmp_path: Path):
    client = CaptureTelegramClient(updates=[])
    service = TelegramService(
        home=tmp_path,
        config=TelegramConfig(enabled=True, bot_token="token", dm_policy="open"),
        runtime=AgentRuntime(home=tmp_path, provider=NoModelCalls()),
        project_dir=tmp_path,
        client=client,
    )
    service.ingress = CaptureIngress()

    from navi.telegram.models import TelegramUpdate

    update = TelegramUpdate(
        update_id=3,
        message_id=30,
        chat_id="100",
        sender_id="200",
        text="",
        attachments=(
            TelegramAttachment(
                kind="image",
                mime_type="image/jpeg",
                file_name="photo.jpg",
                local_path="/tmp/photo.jpg",
            ),
        ),
    )

    handled = await service.handle_update(update)

    assert handled is True
    message = service.ingress.messages[0]
    assert message.text == "[media] photo.jpg"
    facts = message.facts
    assert facts["connector"] == "telegram"
    assert facts["attachment_count"] == 1
    assert facts["attachments"][0]["local_path"] == "/tmp/photo.jpg"
