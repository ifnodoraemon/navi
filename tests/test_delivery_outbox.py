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


def test_claim_ready_prioritizes_interactive_transport_context(tmp_path):
    store = DeliveryOutboxStore(tmp_path)
    background = store.enqueue(
        DeliveryEnvelope(
            batch_id="background",
            channel="test",
            peer_id="peer-1",
            text="background",
            transport_context={"priority": 0},
        )
    )[0]
    interactive = store.enqueue(
        DeliveryEnvelope(
            batch_id="interactive",
            channel="test",
            peer_id="peer-1",
            text="interactive",
            transport_context={"priority": 100},
        )
    )[0]

    claimed = store.claim_ready(channel="test", limit=1)

    assert claimed[0].id == interactive.id
    assert store.get(background.id).status == "pending"


@pytest.mark.asyncio
async def test_expired_delivery_is_failed_without_transport_submission(tmp_path):
    store = DeliveryOutboxStore(tmp_path)
    item = store.enqueue(
        DeliveryEnvelope(
            batch_id="expired",
            channel="test",
            peer_id="peer-1",
            text="stale notification",
            transport_context={"expires_at": 1.0},
        )
    )[0]
    transport = _CaptionTransport()

    outcomes = await DeliveryCoordinator(store).drain(transport)

    assert [outcome.state for outcome in outcomes] == ["expired"]
    assert transport.delivered == []
    persisted = store.get(item.id)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.error.startswith("connector_delivery_expired:")
    with pytest.raises(ValueError, match="expired"):
        store.requeue_failed(item.id)
