from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TelegramUpdate:
    update_id: int
    message_id: int
    chat_id: str
    sender_id: str
    text: str
