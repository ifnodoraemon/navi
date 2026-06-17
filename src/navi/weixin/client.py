from __future__ import annotations

import base64
import json
import secrets
import struct
import uuid
from typing import Any

import httpx

from .models import WeixinAccount, WeixinQr, WeixinUpdate, WeixinUpdateBatch
from .store import extract_text, split_text_for_weixin

ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

ITEM_TEXT = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2
SEND_CHUNK_RETRIES = 3
SEND_CHUNK_RETRY_DELAY_SECONDS = 1.0
TYPING_START = 1
TYPING_STOP = 2
CONFIG_TIMEOUT_SECONDS = 10.0


class WeixinClient:
    def __init__(self, *, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token

    async def request_qr(self) -> WeixinQr:
        data = await self._get("/ilink/bot/get_bot_qrcode?bot_type=3", timeout=35)
        ticket = str(data.get("qrcode") or data.get("ticket") or "")
        qrcode_url = str(
            data.get("qrcode_img_content") or data.get("qrcode_url") or data.get("url") or ticket
        )
        if not ticket:
            raise RuntimeError(f"iLink QR response did not include qrcode: {data}")
        return WeixinQr(qrcode_url=qrcode_url, ticket=ticket)

    async def poll_qr_status(self, ticket: str) -> WeixinAccount | None:
        data = await self._get(f"/ilink/bot/get_qrcode_status?qrcode={ticket}", timeout=35)
        status = str(data.get("status") or "wait").lower()
        if status in {"wait", "scaned", "scaned_but_redirect", "expired"}:
            return None
        if status not in {"confirmed", "success", "authorized", "logged_in"}:
            return None
        account_id = str(
            data.get("ilink_bot_id") or data.get("account_id") or data.get("bot_account_id") or ""
        )
        token = str(data.get("bot_token") or data.get("token") or data.get("access_token") or "")
        base_url = str(data.get("baseurl") or self.base_url).rstrip("/")
        user_id = str(data.get("ilink_user_id") or data.get("user_id") or "")
        if not account_id or not token:
            raise RuntimeError(f"iLink QR confirmed without account_id/token: {data}")
        return WeixinAccount(account_id=account_id, token=token, base_url=base_url, user_id=user_id)

    async def get_updates(self, account_id: str, *, sync_buf: str = "") -> WeixinUpdateBatch:
        data = await self._post(
            "/ilink/bot/getupdates",
            {"get_updates_buf": sync_buf},
            timeout=40,
        )
        raw_updates = data.get("msgs") or data.get("updates") or data.get("items") or []
        updates = []
        for raw in raw_updates:
            if not isinstance(raw, dict):
                continue
            text = extract_text(raw)
            if not text:
                continue
            peer_id, is_group = self._peer_id(raw, account_id)
            sender_id = str(
                raw.get("from_user_id") or raw.get("sender_id") or raw.get("from_user") or peer_id
            )
            updates.append(
                WeixinUpdate(
                    message_id=str(raw.get("message_id") or raw.get("id") or uuid.uuid4().hex),
                    peer_id=peer_id,
                    sender_id=sender_id,
                    text=text,
                    context_token=str(raw.get("context_token") or ""),
                    is_group=is_group,
                )
            )
        return WeixinUpdateBatch(
            updates=updates,
            sync_buf=str(data.get("get_updates_buf") or sync_buf or ""),
            timeout_ms=int(data.get("longpolling_timeout_ms") or 35000),
        )

    async def send_message(
        self,
        *,
        account_id: str,
        peer_id: str,
        text: str,
        context_token: str = "",
    ) -> None:
        chunks = split_text_for_weixin(text)
        for index, chunk in enumerate(chunks):
            await self._send_chunk(peer_id=peer_id, text=chunk, context_token=context_token)
            if index < len(chunks) - 1:
                await self._sleep_between_chunks()

    async def get_typing_ticket(self, *, user_id: str, context_token: str = "") -> str:
        payload: dict[str, Any] = {"ilink_user_id": user_id}
        if context_token:
            payload["context_token"] = context_token
        response = await self._post("/ilink/bot/getconfig", payload, timeout=CONFIG_TIMEOUT_SECONDS)
        return str(response.get("typing_ticket") or "")

    async def send_typing(self, *, peer_id: str, typing_ticket: str, status: int) -> None:
        if not typing_ticket:
            return
        await self._post(
            "/ilink/bot/sendtyping",
            {
                "ilink_user_id": peer_id,
                "typing_ticket": typing_ticket,
                "status": status,
            },
            timeout=CONFIG_TIMEOUT_SECONDS,
        )

    async def _send_chunk(self, *, peer_id: str, text: str, context_token: str = "") -> None:
        if not text.strip():
            raise ValueError("Weixin text must not be empty")
        client_id = f"navi-weixin-{uuid.uuid4().hex}"
        last_error: Exception | None = None
        retried_without_context = False
        for attempt in range(SEND_CHUNK_RETRIES + 1):
            try:
                message = self._message_payload(
                    peer_id=peer_id,
                    text=text,
                    context_token=context_token,
                    client_id=client_id,
                )
                response = await self._post("/ilink/bot/sendmessage", {"msg": message}, timeout=15)
                ret = response.get("ret")
                errcode = response.get("errcode")
                if ret in (None, 0) and errcode in (None, 0):
                    return
                if (
                    _is_session_expired(ret, errcode, response.get("errmsg"))
                    and context_token
                    and not retried_without_context
                ):
                    context_token = ""
                    retried_without_context = True
                    continue
                if _is_rate_limited(ret, errcode) and attempt < SEND_CHUNK_RETRIES:
                    await self._sleep(SEND_CHUNK_RETRY_DELAY_SECONDS * 3)
                    continue
                raise RuntimeError(
                    f"iLink sendmessage error ret={ret} errcode={errcode}: {response}"
                )
            except Exception as exc:
                last_error = exc
                if attempt >= SEND_CHUNK_RETRIES:
                    break
                await self._sleep(SEND_CHUNK_RETRY_DELAY_SECONDS * (attempt + 1))
        if last_error:
            raise last_error

    @staticmethod
    def _message_payload(
        *, peer_id: str, text: str, context_token: str, client_id: str
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": peer_id,
            "client_id": client_id,
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            message["context_token"] = context_token
        return message

    async def _get(self, path: str, *, timeout: float) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout, trust_env=True) as client:
            response = await client.get(f"{self.base_url}{path}", headers=self._headers(""))
            response.raise_for_status()
            data = response.json()
        return self._unwrap(data)

    async def _post(self, path: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        body = json.dumps(
            {**payload, "base_info": {"channel_version": CHANNEL_VERSION}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        async with httpx.AsyncClient(timeout=timeout, trust_env=True) as client:
            response = await client.post(
                f"{self.base_url}{path}", content=body, headers=self._headers(body)
            )
            response.raise_for_status()
            data = response.json()
        return self._unwrap(data)

    def _headers(self, body: str) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Content-Length": str(len(body.encode("utf-8"))),
            "X-WECHAT-UIN": _random_wechat_uin(),
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _unwrap(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected iLink response: {data!r}")
        if "data" in data and isinstance(data["data"], dict):
            return data["data"]
        return data

    @staticmethod
    def _peer_id(raw: dict[str, Any], account_id: str) -> tuple[str, bool]:
        room_id = str(raw.get("room_id") or raw.get("chat_room_id") or "").strip()
        to_user_id = str(raw.get("to_user_id") or "").strip()
        is_group = bool(room_id) or bool(raw.get("is_group")) or to_user_id.endswith("@chatroom")
        if is_group:
            return room_id or to_user_id or str(
                raw.get("peer_id") or raw.get("chat_id") or ""
            ), True
        return str(
            raw.get("peer_id")
            or raw.get("chat_id")
            or raw.get("from_user_id")
            or raw.get("from_user")
            or ""
        ), False

    async def _sleep_between_chunks(self) -> None:
        await self._sleep(0.3)

    async def _sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _is_rate_limited(ret: Any, errcode: Any) -> bool:
    return ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE


def _is_session_expired(ret: Any, errcode: Any, errmsg: Any) -> bool:
    if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE:
        return True
    if not _is_rate_limited(ret, errcode):
        return False
    return str(errmsg or "").lower() == "unknown error"
