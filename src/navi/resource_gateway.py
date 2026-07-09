from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from .loop_contracts import BudgetState, ResourceDecision


@dataclass(frozen=True)
class ResourceLimits:
    token_budget: int = 0
    call_budget: int = 0
    cost_budget: float = 0.0
    qps_limit: int = 0
    max_concurrent: int = 1


@dataclass(frozen=True)
class ResourceRequest:
    kind: str
    estimated_tokens: int = 0
    estimated_cost: float = 0.0
    units: int = 1


@dataclass(frozen=True)
class ResourceGrant:
    decision: ResourceDecision | str
    reason: str
    budget_state: BudgetState
    retry_after_seconds: float = 0.0

    @property
    def allowed(self) -> bool:
        return self.decision == ResourceDecision.ALLOW

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": str(self.decision),
            "reason": self.reason,
            "budget_state": self.budget_state.to_dict(),
            "retry_after_seconds": self.retry_after_seconds,
        }


class GlobalResourceGateway:
    """Deterministic resource guard for LLM/tool/subtask/API calls.

    This is a control-plane component, not a policy brain. It receives declared
    request costs and returns facts: allow, pause, escalate, or block.
    """

    def __init__(self, limits: ResourceLimits):
        self.limits = limits
        self._used_tokens = 0
        self._used_calls = 0
        self._used_cost = 0.0
        self._active = 0
        self._call_times: deque[float] = deque()

    def inspect(self) -> BudgetState:
        return BudgetState(
            decision=ResourceDecision.ALLOW,
            token_budget_remaining=_remaining(self.limits.token_budget, self._used_tokens),
            call_budget_remaining=_remaining(self.limits.call_budget, self._used_calls),
            cost_budget_remaining=_remaining_float(self.limits.cost_budget, self._used_cost),
            reason="ok",
        )

    def request(self, request: ResourceRequest, *, now: float | None = None) -> ResourceGrant:
        current = time.time() if now is None else now
        if request.units < 1:
            return self._grant(ResourceDecision.BLOCK, "invalid_units")
        if self.limits.max_concurrent > 0 and self._active + request.units > self.limits.max_concurrent:
            return self._grant(ResourceDecision.PAUSE, "concurrency_limit")
        if self.limits.call_budget > 0 and self._used_calls + request.units > self.limits.call_budget:
            return self._grant(ResourceDecision.ESCALATE, "call_budget_exhausted")
        if self.limits.token_budget > 0 and self._used_tokens + request.estimated_tokens > self.limits.token_budget:
            return self._grant(ResourceDecision.ESCALATE, "token_budget_exhausted")
        if self.limits.cost_budget > 0 and self._used_cost + request.estimated_cost > self.limits.cost_budget:
            return self._grant(ResourceDecision.ESCALATE, "cost_budget_exhausted")

        retry_after = self._qps_retry_after(current)
        if retry_after > 0:
            grant = self._grant(ResourceDecision.PAUSE, "rate_limited")
            return ResourceGrant(
                decision=grant.decision,
                reason=grant.reason,
                budget_state=grant.budget_state,
                retry_after_seconds=retry_after,
            )

        self._used_calls += request.units
        self._used_tokens += max(0, request.estimated_tokens)
        self._used_cost += max(0.0, request.estimated_cost)
        self._active += request.units
        for _ in range(request.units):
            self._call_times.append(current)
        return self._grant(ResourceDecision.ALLOW, "ok")

    def release(self, *, units: int = 1) -> BudgetState:
        if units > 0:
            self._active = max(0, self._active - units)
        return self.inspect()

    def provider_rate_limited(self, *, retry_after_seconds: float = 0.0) -> ResourceGrant:
        return ResourceGrant(
            decision=ResourceDecision.PAUSE,
            reason="provider_rate_limited",
            budget_state=self._budget_state(ResourceDecision.PAUSE, "provider_rate_limited"),
            retry_after_seconds=max(0.0, retry_after_seconds),
        )

    def _grant(self, decision: ResourceDecision, reason: str) -> ResourceGrant:
        return ResourceGrant(
            decision=decision,
            reason=reason,
            budget_state=self._budget_state(decision, reason),
        )

    def _budget_state(self, decision: ResourceDecision, reason: str) -> BudgetState:
        return BudgetState(
            decision=decision,
            token_budget_remaining=_remaining(self.limits.token_budget, self._used_tokens),
            call_budget_remaining=_remaining(self.limits.call_budget, self._used_calls),
            cost_budget_remaining=_remaining_float(self.limits.cost_budget, self._used_cost),
            reason=reason,
        )

    def _qps_retry_after(self, now: float) -> float:
        if self.limits.qps_limit <= 0:
            return 0.0
        while self._call_times and now - self._call_times[0] >= 1.0:
            self._call_times.popleft()
        if len(self._call_times) < self.limits.qps_limit:
            return 0.0
        return max(0.0, 1.0 - (now - self._call_times[0]))


def _remaining(limit: int, used: int) -> int | None:
    if limit <= 0:
        return None
    return max(0, limit - used)


def _remaining_float(limit: float, used: float) -> float | None:
    if limit <= 0:
        return None
    return max(0.0, limit - used)
