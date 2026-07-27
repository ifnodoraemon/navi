"""Weixin implementation of Navi's connector-neutral delivery transport."""

from __future__ import annotations

from pathlib import Path

import httpx

from navi.delivery_outbox import DeliveryFailure, DeliveryItem, DeliveryReceipt

from .client import WeixinClient, WeixinTransportError
from .models import WeixinAccount


class WeixinDeliveryTransport:
    """Translate a generic outbox item into one iLink request.

    The generic coordinator owns queueing, idempotency lifetime, retries and
    persisted receipts.  This adapter owns only Weixin request shape and which
    of its failures are transient.
    """

    channel = "weixin"

    def __init__(self, *, client: WeixinClient, account: WeixinAccount, channel: str = "weixin"):
        self.client = client
        self.account = account
        self.channel = channel

    async def deliver(self, item: DeliveryItem) -> DeliveryReceipt:
        payload = item.payload
        context_token = str(item.transport_context.get("context_token") or "")
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
            return DeliveryFailure(
                reason=exc.reason,
                error=f"{type(exc).__name__}: {exc}",
                retryable=False,
            )
        return DeliveryFailure(
            reason="connector_delivery_failed",
            error=f"{type(exc).__name__}: {exc}",
            retryable=False,
        )
