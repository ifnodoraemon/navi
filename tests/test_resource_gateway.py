from __future__ import annotations

from navi.loop_contracts import ResourceDecision
from navi.resource_gateway import (
    GlobalResourceGateway,
    ResourceLimits,
    ResourceRequest,
    SQLiteResourceLedger,
)


def test_resource_gateway_tracks_budget_and_pauses_on_concurrency():
    gateway = GlobalResourceGateway(
        ResourceLimits(token_budget=100, call_budget=2, cost_budget=1.5, max_concurrent=1)
    )

    first = gateway.request(
        ResourceRequest(kind="llm", estimated_tokens=40, estimated_cost=0.5),
        now=100.0,
    )
    assert first.allowed is True
    assert first.budget_state.token_budget_remaining == 60
    assert first.budget_state.call_budget_remaining == 1
    assert first.budget_state.cost_budget_remaining == 1.0

    concurrent = gateway.request(ResourceRequest(kind="tool"), now=100.1)
    assert concurrent.decision == ResourceDecision.PAUSE
    assert concurrent.reason == "concurrency_limit"

    gateway.release()
    second = gateway.request(ResourceRequest(kind="tool", estimated_tokens=10), now=100.2)
    assert second.allowed is True
    assert second.budget_state.call_budget_remaining == 0

    gateway.release()
    exhausted = gateway.request(ResourceRequest(kind="tool"), now=100.3)
    assert exhausted.decision == ResourceDecision.ESCALATE
    assert exhausted.reason == "call_budget_exhausted"


def test_resource_gateway_rate_limit_and_provider_429_are_pause_facts():
    gateway = GlobalResourceGateway(ResourceLimits(qps_limit=1, max_concurrent=10))

    assert gateway.request(ResourceRequest(kind="llm"), now=200.0).allowed is True
    rate_limited = gateway.request(ResourceRequest(kind="llm"), now=200.2)
    assert rate_limited.decision == ResourceDecision.PAUSE
    assert rate_limited.reason == "rate_limited"
    assert rate_limited.retry_after_seconds > 0

    provider_limit = gateway.provider_rate_limited(retry_after_seconds=3.5)
    assert provider_limit.decision == ResourceDecision.PAUSE
    assert provider_limit.reason == "provider_rate_limited"
    assert provider_limit.retry_after_seconds == 3.5


def test_sqlite_resource_ledger_survives_gateway_recreation_and_reconciles_usage(tmp_path):
    limits = ResourceLimits(token_budget=100, call_budget=2, max_concurrent=1)
    first_gateway = GlobalResourceGateway(
        limits,
        ledger=SQLiteResourceLedger(tmp_path),
        scope_id="loop-1",
    )

    first = first_gateway.request(ResourceRequest(kind="llm", estimated_tokens=20))
    assert first.allowed is True
    first_gateway.release(grant_id=first.grant_id, actual_tokens=35)

    resumed_gateway = GlobalResourceGateway(
        limits,
        ledger=SQLiteResourceLedger(tmp_path),
        scope_id="loop-1",
    )
    resumed = resumed_gateway.inspect()
    assert resumed.token_budget_remaining == 65
    assert resumed.call_budget_remaining == 1

    second = resumed_gateway.request(ResourceRequest(kind="tool", estimated_tokens=10))
    assert second.allowed is True
    resumed_gateway.release(grant_id=second.grant_id)
    exhausted = resumed_gateway.request(ResourceRequest(kind="tool"))
    assert exhausted.reason == "call_budget_exhausted"


def test_cost_budget_fails_closed_without_declared_pricing(tmp_path):
    gateway = GlobalResourceGateway(
        ResourceLimits(cost_budget=1.0),
        ledger=SQLiteResourceLedger(tmp_path),
        scope_id="priced-loop",
    )

    grant = gateway.request(ResourceRequest(kind="llm", estimated_cost=0.0))

    assert grant.decision == ResourceDecision.BLOCK
    assert grant.reason == "cost_estimate_required"
