from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WeixinAccount:
    account_id: str
    token: str
    base_url: str
    user_id: str = ""


@dataclass(frozen=True)
class WeixinQr:
    qrcode_url: str
    ticket: str


@dataclass(frozen=True)
class WeixinUpdate:
    message_id: str
    peer_id: str
    sender_id: str
    text: str
    context_token: str = ""
    is_group: bool = False


@dataclass(frozen=True)
class WeixinUpdateBatch:
    updates: list[WeixinUpdate]
    sync_buf: str = ""
    timeout_ms: int = 35000
