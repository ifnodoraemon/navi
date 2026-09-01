from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TelegramAttachment:
    kind: str
    mime_type: str
    file_name: str
    size: int = 0
    file_id: str = ""
    local_path: str = ""
    download_error: str = ""


@dataclass(frozen=True)
class TelegramUpdate:
    update_id: int
    message_id: int
    chat_id: str
    sender_id: str
    text: str
    attachments: tuple[TelegramAttachment, ...] = ()
