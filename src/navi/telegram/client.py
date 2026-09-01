from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path
from typing import Any

import httpx

from .models import TelegramAttachment, TelegramUpdate

_GET_FILE_TIMEOUT_SECONDS = 15.0
_DOWNLOAD_TIMEOUT_SECONDS = 60.0

# Telegram media message fields in priority order; the first match wins so a
# message carrying both a document and a photo surfaces one primary payload.
_MEDIA_FIELDS: tuple[tuple[str, str], ...] = (
    ("document", "file"),
    ("video", "video"),
    ("video_note", "video-note"),
    ("voice", "voice"),
    ("audio", "audio"),
    ("animation", "animation"),
    ("sticker", "sticker"),
    ("photo", "image"),
)


class TelegramClient:
    def __init__(
        self,
        *,
        api_base_url: str,
        bot_token: str,
        media_dir: Path | None = None,
    ):
        self.api_base_url = api_base_url.rstrip("/")
        self.bot_token = bot_token
        self.media_dir = media_dir

    async def get_updates(
        self, *, offset: int | None = None, timeout: int = 25
    ) -> list[TelegramUpdate]:
        data: dict[str, object] = {"timeout": timeout}
        if offset is not None:
            data["offset"] = offset
        async with httpx.AsyncClient(timeout=timeout + 10) as client:
            response = await client.post(self._url("getUpdates"), json=data)
            response.raise_for_status()
            payload = response.json()
        updates: list[TelegramUpdate] = []
        for item in payload.get("result") or []:
            parsed = _parse_update(item)
            if parsed is not None:
                updates.append(parsed)
        if self.media_dir is not None:
            updates = [
                await self._with_downloaded_attachments(update) for update in updates
            ]
        return updates

    async def send_message(self, *, chat_id: str, text: str) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self._url("sendMessage"), json={"chat_id": chat_id, "text": text}
            )
            response.raise_for_status()

    async def _with_downloaded_attachments(
        self, update: TelegramUpdate
    ) -> TelegramUpdate:
        from dataclasses import replace

        media_dir = self.media_dir
        if media_dir is None or not update.attachments:
            return update
        downloaded: list[TelegramAttachment] = []
        for index, attachment in enumerate(update.attachments):
            if not attachment.file_id:
                downloaded.append(
                    replace(attachment, download_error="attachment missing file_id")
                )
                continue
            try:
                file_path = await self._get_file_path(attachment.file_id)
                if not file_path:
                    raise ValueError("getFile returned no file_path")
                url = f"{self.api_base_url}/file/bot{self.bot_token}/{file_path}"
                async with httpx.AsyncClient(
                    timeout=_DOWNLOAD_TIMEOUT_SECONDS
                ) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    data = response.content
                safe_name = _sanitize_attachment_name(
                    attachment.file_name,
                    fallback=f"attachment-{index}{Path(file_path).suffix or ''}",
                )
                saved = media_dir / f"{update.chat_id}-{update.message_id}-{index}-{safe_name}"
                _write_bytes_atomically(saved, data)
                downloaded.append(
                    replace(
                        attachment,
                        local_path=str(saved),
                        size=len(data),
                    )
                )
            except Exception as exc:
                downloaded.append(
                    replace(
                        attachment,
                        download_error=f"{type(exc).__name__}: {exc}",
                    )
                )
        return replace(update, attachments=tuple(downloaded))

    async def _get_file_path(self, file_id: str) -> str:
        async with httpx.AsyncClient(timeout=_GET_FILE_TIMEOUT_SECONDS) as client:
            response = await client.post(
                self._url("getFile"), json={"file_id": file_id}
            )
            response.raise_for_status()
            payload = response.json()
        result = payload.get("result") or {}
        return str(result.get("file_path") or "") if isinstance(result, dict) else ""

    def _url(self, method: str) -> str:
        return f"{self.api_base_url}/bot{self.bot_token}/{method}"


def _parse_update(item: object) -> TelegramUpdate | None:
    if not isinstance(item, dict):
        return None
    message = item.get("message") or item.get("edited_message")
    if not isinstance(message, dict):
        return None
    text = str(message.get("text") or message.get("caption") or "").strip()
    attachments = _attachments_from_message(message)
    if not text and not attachments:
        return None
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None
    return TelegramUpdate(
        update_id=int(item.get("update_id") or 0),
        message_id=int(message.get("message_id") or 0),
        chat_id=str(chat.get("id") or ""),
        sender_id=str(sender.get("id") or ""),
        text=text,
        attachments=attachments,
    )


def _attachments_from_message(message: dict[str, Any]) -> tuple[TelegramAttachment, ...]:
    attachments: list[TelegramAttachment] = []
    for field, kind in _MEDIA_FIELDS:
        payload = message.get(field)
        if payload is None:
            continue
        attachment = _attachment_from_payload(field, kind, payload)
        if attachment is not None:
            attachments.append(attachment)
            break
    return tuple(attachments)


def _attachment_from_payload(
    field: str, kind: str, payload: object
) -> TelegramAttachment | None:
    if field == "photo":
        # PhotoSize array; the last entry is the largest resolution.
        if not isinstance(payload, list) or not payload:
            return None
        sizes = [item for item in payload if isinstance(item, dict)]
        if not sizes:
            return None
        largest = max(
            sizes,
            key=lambda item: (int(item.get("width") or 0), int(item.get("height") or 0)),
        )
        payload = largest
        file_name = "photo.jpg"
        mime_type = "image/jpeg"
    else:
        if not isinstance(payload, dict):
            return None
        file_name = str(payload.get("file_name") or "")
        mime_type = str(payload.get("mime_type") or "")
        if field == "sticker" and not file_name:
            file_name = f"sticker{str(payload.get('emoji') or '')}".strip()
        if field == "voice" and not file_name:
            file_name = "voice.ogg"
        if field == "video_note" and not file_name:
            file_name = "video-note.mp4"
    if not mime_type:
        mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    return TelegramAttachment(
        kind=kind,
        mime_type=mime_type,
        file_name=file_name or "attachment.bin",
        size=_coerce_int(payload.get("file_size") if isinstance(payload, dict) else None),
        file_id=str(payload.get("file_id") or "") if isinstance(payload, dict) else "",
    )


def _coerce_int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _sanitize_attachment_name(name: str, *, fallback: str) -> str:
    base = str(name or "").replace("\\", "/").split("/")[-1]
    cleaned = "".join(
        ch if ch.isprintable() and ch not in '<>:"|?*' else "_" for ch in base
    ).strip(" .")
    cleaned = cleaned[:150]
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or fallback


def _write_bytes_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
