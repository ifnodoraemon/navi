from __future__ import annotations

import httpx

from .models import TelegramUpdate


class TelegramClient:
    def __init__(self, *, api_base_url: str, bot_token: str):
        self.api_base_url = api_base_url.rstrip("/")
        self.bot_token = bot_token

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
        return updates

    async def send_message(self, *, chat_id: str, text: str) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self._url("sendMessage"), json={"chat_id": chat_id, "text": text}
            )
            response.raise_for_status()

    def _url(self, method: str) -> str:
        return f"{self.api_base_url}/bot{self.bot_token}/{method}"


def _parse_update(item: object) -> TelegramUpdate | None:
    if not isinstance(item, dict):
        return None
    message = item.get("message") or item.get("edited_message")
    if not isinstance(message, dict):
        return None
    text = str(message.get("text") or "").strip()
    if not text:
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
    )
