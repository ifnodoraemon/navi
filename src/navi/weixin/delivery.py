"""Weixin implementation of Navi's connector-neutral delivery transport."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from navi.delivery_outbox import DeliveryFailure, DeliveryItem, DeliveryReceipt

from .client import WeixinClient, WeixinTransportError
from .models import WeixinAccount
from .store import WeixinSessionStore

WEIXIN_RATE_LIMIT_RETRY_SECONDS = 900.0


class WeixinDeliveryTransport:
    """Translate a generic outbox item into one iLink request.

    The generic coordinator owns queueing, idempotency lifetime, retries and
    persisted receipts.  This adapter owns only Weixin request shape and which
    of its failures are transient.
    """

    channel = "weixin"

    def __init__(
        self,
        *,
        client: WeixinClient,
        account: WeixinAccount,
        channel: str = "weixin",
        sessions: WeixinSessionStore | None = None,
        send_lock: asyncio.Lock | None = None,
    ):
        self.client = client
        self.account = account
        self.channel = channel
        self.sessions = sessions
        self.send_lock = send_lock

    async def deliver(self, item: DeliveryItem) -> DeliveryReceipt:
        payload = item.payload
        embedded_token = str(item.transport_context.get("context_token") or "")
        context_token = (
            self.sessions.resolve(
                self.account.account_id,
                item.peer_id,
                fallback=embedded_token,
            )
            if self.sessions is not None
            else embedded_token
        )
        if self.send_lock is not None:
            async with self.send_lock:
                return await self._deliver_locked(item, payload, context_token)
        return await self._deliver_locked(item, payload, context_token)

    async def _deliver_locked(
        self,
        item: DeliveryItem,
        payload: dict,
        context_token: str,
    ) -> DeliveryReceipt:
        try:
            return await self._deliver_once(item, payload, context_token)
        except WeixinTransportError as exc:
            if exc.reason != "connector_session_expired" or not context_token:
                raise
            if self.sessions is not None:
                self.sessions.invalidate(
                    self.account.account_id,
                    item.peer_id,
                    reason=exc.reason,
                )
            receipt = await self._deliver_once(item, payload, "")
            return DeliveryReceipt(
                transport=receipt.transport,
                media_count=receipt.media_count,
                details={**(receipt.details or {}), "retried_without_context_token": True},
            )

    async def _deliver_once(
        self,
        item: DeliveryItem,
        payload: dict,
        context_token: str,
    ) -> DeliveryReceipt:
        if item.kind == "text":
            text = str(payload.get("text") or "").strip()
            if not text:
                raise ValueError("delivery text payload is empty")
            await self.client.send_message(
                account_id=self.account.account_id,
                peer_id=item.peer_id,
                text=text,
                context_token=context_token,
                idempotency_key=item.id,
            )
            return DeliveryReceipt(transport="ilink_sendmessage_completed")
        if item.kind == "file":
            path = Path(str(payload.get("path") or "")).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(f"connector delivery file not found: {path}")
            await self.client.send_file(
                account_id=self.account.account_id,
                peer_id=item.peer_id,
                file_path=path,
                context_token=context_token,
                idempotency_key=item.id,
            )
            return DeliveryReceipt(
                transport="ilink_sendmessage_completed",
                media_count=1,
                details={"path": str(path)},
            )
        raise ValueError(f"unsupported Weixin delivery kind: {item.kind}")

    def classify_failure(self, exc: Exception) -> DeliveryFailure:
        if isinstance(exc, (httpx.TransportError, TimeoutError)):
            return DeliveryFailure(
                reason="connector_transport_unavailable",
                error=f"{type(exc).__name__}: {exc}",
                retryable=True,
            )
        if isinstance(exc, WeixinTransportError):
            retryable = exc.reason == "connector_rate_limited"
            return DeliveryFailure(
                reason=exc.reason,
                error=f"{type(exc).__name__}: {exc}",
                retryable=retryable,
                retry_after_seconds=WEIXIN_RATE_LIMIT_RETRY_SECONDS if retryable else 0.0,
                provider_code=f"ret={exc.ret} errcode={exc.errcode}",
            )
        return DeliveryFailure(
            reason="connector_delivery_failed",
            error=f"{type(exc).__name__}: {exc}",
            retryable=False,
        )
