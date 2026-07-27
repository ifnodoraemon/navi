from __future__ import annotations

import pytest

from navi.delivery_outbox import (
    DeliveryCoordinator,
    DeliveryEnvelope,
    DeliveryFailure,
    DeliveryOutboxStore,
    DeliveryReceipt,
)


class _CaptionTransport:
    channel = "test"

    def __init__(self) -> None:
        self.delivered: list[str] = []

    async def deliver(self, item):
        self.delivered.append(item.id)
        if item.kind == "text":
            raise ConnectionError("caption connection reset")
        return DeliveryReceipt(transport="test", media_count=1)

    def classify_failure(self, exc: Exception) -> DeliveryFailure:
        return DeliveryFailure(
            reason="transport_unavailable",
            error=f"{type(exc).__name__}: {exc}",
            retryable=True,
        )


@pytest.mark.asyncio
async def test_retryable_text_failure_does_not_skip_an_independent_file_item(tmp_path):
    store = DeliveryOutboxStore(tmp_path)
    items = store.enqueue(
        DeliveryEnvelope(
            batch_id="report-1",
            channel="test",
            peer_id="peer-1",
            text="caption",
            file_path="/tmp/report.png",
        )
    )

    transport = _CaptionTransport()
    outcomes = await DeliveryCoordinator(store).drain(transport)

    assert [outcome.state for outcome in outcomes] == ["retry_scheduled", "sent"]
    assert transport.delivered == ["report-1:text", "report-1:file"]
    persisted = {item.id: item for item in store.list_batch(items[0].batch_id)}
    assert persisted["report-1:text"].status == "pending"
    assert persisted["report-1:text"].attempts == 1
    assert persisted["report-1:file"].status == "sent"
    assert persisted["report-1:file"].receipt["media_count"] == 1
