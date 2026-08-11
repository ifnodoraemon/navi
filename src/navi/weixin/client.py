from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import secrets
import struct
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..connector_contract import SYNTHETIC_MESSAGE_ID_PREFIX
from .config import DEFAULT_WEIXIN_CDN_BASE_URL
from .models import (
    WeixinAccount,
    WeixinAttachment,
    WeixinQr,
    WeixinUpdate,
    WeixinUpdateBatch,
)
from .store import extract_text, split_text_for_weixin

ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3
MEDIA_VOICE = 4

MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
TYPING_START = 1
TYPING_STOP = 2
CONFIG_TIMEOUT_SECONDS = 10.0
SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2


class WeixinTransportError(RuntimeError):
    """Typed iLink rejection facts; callers decide whether/how to recover."""

    def __init__(
        self,
        operation: str,
        *,
        ret: Any = None,
        errcode: Any = None,
        errmsg: Any = None,
    ) -> None:
        self.operation = operation
        self.ret = ret
        self.errcode = errcode
        self.errmsg = " ".join(str(errmsg or "").split())[:300]
        self.reason = _ilink_error_reason(ret=ret, errcode=errcode, errmsg=self.errmsg)
        super().__init__(
            f"iLink {operation} rejected ret={ret} errcode={errcode}"
            + (f" errmsg={self.errmsg}" if self.errmsg else "")
        )


class WeixinClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        cdn_base_url: str = DEFAULT_WEIXIN_CDN_BASE_URL,
        media_dir: Path | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.cdn_base_url = cdn_base_url.rstrip("/")
        self.token = token
        self.media_dir = media_dir

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
            native_id = raw.get("message_id") or raw.get("id")
            message_id = str(native_id) if native_id else f"{SYNTHETIC_MESSAGE_ID_PREFIX}{uuid.uuid4().hex}"
            attachments = await self._attachments_from_raw(raw, message_id=message_id)
            if not text and not attachments:
                continue
            peer_id, is_group = self._peer_id(raw, account_id)
            sender_id = str(
                raw.get("from_user_id") or raw.get("sender_id") or raw.get("from_user") or peer_id
            )
            updates.append(
                WeixinUpdate(
                    message_id=message_id,
                    peer_id=peer_id,
                    sender_id=sender_id,
                    text=text,
                    context_token=str(raw.get("context_token") or ""),
                    is_group=is_group,
                    attachments=tuple(attachments),
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
        idempotency_key: str = "",
    ) -> None:
        chunks = split_text_for_weixin(text)
        for index, chunk in enumerate(chunks):
            await self._send_chunk(
                peer_id=peer_id,
                text=chunk,
                context_token=context_token,
                idempotency_key=(f"{idempotency_key}:chunk:{index}" if idempotency_key else ""),
            )
            if index < len(chunks) - 1:
                await self._sleep_between_chunks()

    async def send_file(
        self,
        *,
        account_id: str,
        peer_id: str,
        file_path: str | Path,
        caption: str = "",
        context_token: str = "",
        force_file_attachment: bool = False,
        idempotency_key: str = "",
    ) -> None:
        del account_id
        path = Path(file_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Weixin file not found: {path}")
        if not path.is_file():
            raise ValueError(f"Weixin file path is not a file: {path}")
        if caption.strip():
            await self.send_message(
                account_id="",
                peer_id=peer_id,
                text=caption,
                context_token=context_token,
            )
        item = await self._upload_media_item(
            peer_id=peer_id,
            path=path,
            force_file_attachment=force_file_attachment,
        )
        if idempotency_key:
            from navi.connector_delivery import connector_delivery_client_id

            client_id = connector_delivery_client_id(
                idempotency_key,
                prefix="navi-weixin",
            )
        else:
            client_id = f"navi-weixin-{uuid.uuid4().hex}"
        response = await self._post(
            "/ilink/bot/sendmessage",
            {
                "msg": self._message_payload(
                    peer_id=peer_id,
                    item_list=[item],
                    context_token=context_token,
                    client_id=client_id,
                )
            },
            timeout=15,
        )
        _raise_ilink_error(response, "sendmessage")

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

    async def _send_chunk(
        self,
        *,
        peer_id: str,
        text: str,
        context_token: str = "",
        idempotency_key: str = "",
    ) -> None:
        if not text.strip():
            raise ValueError("Weixin text must not be empty")
        if idempotency_key:
            from navi.connector_delivery import connector_delivery_client_id

            client_id = connector_delivery_client_id(idempotency_key, prefix="navi-weixin")
        else:
            client_id = f"navi-weixin-{uuid.uuid4().hex}"
        message = self._message_payload(
            peer_id=peer_id,
            text=text,
            context_token=context_token,
            client_id=client_id,
        )
        response = await self._post("/ilink/bot/sendmessage", {"msg": message}, timeout=15)
        ret = response.get("ret")
        errcode = response.get("errcode")
        if ret not in (None, 0) or errcode not in (None, 0):
            raise WeixinTransportError(
                "sendmessage",
                ret=ret,
                errcode=errcode,
                errmsg=response.get("errmsg"),
            )

    @staticmethod
    def _message_payload(
        *,
        peer_id: str,
        context_token: str,
        client_id: str,
        text: str = "",
        item_list: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        items = (
            item_list
            if item_list is not None
            else [{"type": ITEM_TEXT, "text_item": {"text": text}}]
        )
        message: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": peer_id,
            "client_id": client_id,
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": items,
        }
        if context_token:
            message["context_token"] = context_token
        return message

    async def _upload_media_item(
        self, *, peer_id: str, path: Path, force_file_attachment: bool
    ) -> dict[str, Any]:
        plaintext = path.read_bytes()
        media_type, item_builder = _outbound_media_builder(
            path, force_file_attachment=force_file_attachment
        )
        filekey = secrets.token_hex(16)
        aes_key = secrets.token_bytes(16)
        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()
        upload_response = await self._post(
            "/ilink/bot/getuploadurl",
            {
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": peer_id,
                "rawsize": rawsize,
                "rawfilemd5": rawfilemd5,
                "filesize": _aes_padded_size(rawsize),
                "no_need_thumb": True,
                "aeskey": aes_key.hex(),
            },
            timeout=15,
        )
        _raise_ilink_error(upload_response, "getuploadurl")
        upload_param = str(upload_response.get("upload_param") or "")
        upload_full_url = str(upload_response.get("upload_full_url") or "")
        if upload_full_url:
            upload_url = upload_full_url
        elif upload_param:
            upload_url = _cdn_upload_url(self.cdn_base_url, upload_param, filekey)
        else:
            raise RuntimeError(f"iLink getuploadurl returned no upload target: {upload_response}")
        encrypted_query_param = await self._upload_ciphertext(
            upload_url=upload_url,
            ciphertext=_aes128_ecb_encrypt(plaintext, aes_key),
        )
        aes_key_for_api = base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")
        return item_builder(
            encrypt_query_param=encrypted_query_param,
            aes_key_for_api=aes_key_for_api,
            ciphertext_size=_aes_padded_size(rawsize),
            plaintext_size=rawsize,
            filename=path.name,
            rawfilemd5=rawfilemd5,
        )

    async def _upload_ciphertext(self, *, upload_url: str, ciphertext: bytes) -> str:
        async with httpx.AsyncClient(timeout=120, trust_env=True) as client:
            response = await client.post(
                upload_url,
                content=ciphertext,
                headers={"Content-Type": "application/octet-stream"},
            )
            response.raise_for_status()
            encrypted_param = response.headers.get("x-encrypted-param")
            if encrypted_param:
                return encrypted_param
            raise RuntimeError(
                f"Weixin CDN upload missing x-encrypted-param: {response.text[:200]}"
            )

    async def _attachments_from_raw(
        self, raw: dict[str, Any], *, message_id: str
    ) -> list[WeixinAttachment]:
        items = raw.get("item_list")
        if not isinstance(items, list):
            return []
        attachments: list[WeixinAttachment] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            attachment = await self._attachment_from_item(item, message_id=message_id, index=index)
            if attachment is not None:
                attachments.append(attachment)
        return attachments

    async def _attachment_from_item(
        self, item: dict[str, Any], *, message_id: str, index: int
    ) -> WeixinAttachment | None:
        item_type = item.get("type")
        if item_type == ITEM_IMAGE:
            media = (item.get("image_item") or {}).get("media") or {}
            return WeixinAttachment(
                kind="image",
                mime_type="image/jpeg",
                file_name=f"{message_id}-{index}.jpg",
                media_id=_media_id(media),
                item_type=ITEM_IMAGE,
            )
        if item_type == ITEM_VIDEO:
            video_item = item.get("video_item") or {}
            media = video_item.get("media") or {}
            return WeixinAttachment(
                kind="video",
                mime_type="video/mp4",
                file_name=f"{message_id}-{index}.mp4",
                size=int(video_item.get("video_size") or 0),
                media_id=_media_id(media),
                item_type=ITEM_VIDEO,
            )
        if item_type == ITEM_FILE:
            file_item = item.get("file_item") or {}
            media = file_item.get("media") or {}
            file_name = str(file_item.get("file_name") or f"{message_id}-{index}.bin")
            return WeixinAttachment(
                kind="file",
                mime_type=mimetypes.guess_type(file_name)[0] or "application/octet-stream",
                file_name=file_name,
                size=_coerce_int(file_item.get("len") or file_item.get("size")),
                media_id=_media_id(media),
                item_type=ITEM_FILE,
            )
        return None

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


def _cdn_upload_url(cdn_base_url: str, upload_param: str, filekey: str) -> str:
    return (
        f"{cdn_base_url.rstrip('/')}/upload"
        f"?encrypted_query_param={quote(upload_param, safe='')}"
        f"&filekey={quote(filekey, safe='')}"
    )


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()


def _aes_padded_size(size: int) -> int:
    return ((size + 1 + 15) // 16) * 16


def _outbound_media_builder(path: Path, *, force_file_attachment: bool = False):
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if mime.startswith("image/") and not force_file_attachment:
        return MEDIA_IMAGE, lambda **kw: {
            "type": ITEM_IMAGE,
            "image_item": {
                "media": {
                    "encrypt_query_param": kw["encrypt_query_param"],
                    "aes_key": kw["aes_key_for_api"],
                    "encrypt_type": 1,
                },
                "mid_size": kw["ciphertext_size"],
            },
        }
    if mime.startswith("video/") and not force_file_attachment:
        return MEDIA_VIDEO, lambda **kw: {
            "type": ITEM_VIDEO,
            "video_item": {
                "media": {
                    "encrypt_query_param": kw["encrypt_query_param"],
                    "aes_key": kw["aes_key_for_api"],
                    "encrypt_type": 1,
                },
                "video_size": kw["ciphertext_size"],
                "play_length": 0,
                "video_md5": kw["rawfilemd5"],
            },
        }
    return MEDIA_FILE, lambda **kw: {
        "type": ITEM_FILE,
        "file_item": {
            "media": {
                "encrypt_query_param": kw["encrypt_query_param"],
                "aes_key": kw["aes_key_for_api"],
                "encrypt_type": 1,
            },
            "file_name": kw["filename"],
            "len": str(kw["plaintext_size"]),
        },
    }


def _media_id(media: dict[str, Any]) -> str:
    for key in ("encrypt_query_param", "full_url", "file_id", "media_id"):
        value = str(media.get(key) or "")
        if value:
            return value
    return ""


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _raise_ilink_error(response: dict[str, Any], operation: str) -> None:
    ret = response.get("ret")
    errcode = response.get("errcode")
    if ret in (None, 0) and errcode in (None, 0):
        return
    raise WeixinTransportError(
        operation,
        ret=ret,
        errcode=errcode,
        errmsg=response.get("errmsg"),
    )


def _ilink_error_reason(*, ret: Any, errcode: Any, errmsg: str = "") -> str:
    codes = {_coerce_int(ret), _coerce_int(errcode)}
    normalized_message = errmsg.strip().lower()
    if SESSION_EXPIRED_ERRCODE in codes or (
        RATE_LIMIT_ERRCODE in codes and normalized_message == "unknown error"
    ):
        return "connector_session_expired"
    if RATE_LIMIT_ERRCODE in codes and any(
        marker in normalized_message
        for marker in ("rate limit", "too many request", "too frequent", "频繁")
    ):
        return "connector_rate_limited"
    if RATE_LIMIT_ERRCODE in codes:
        # iLink also uses -2 for opaque preparation rejections.  Preserve that
        # provider fact instead of claiming every -2 is a rate-limit event.
        return "connector_transient_rejected"
    return "connector_rejected"
