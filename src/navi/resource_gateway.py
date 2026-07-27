from __future__ import annotations

import sqlite3
import time
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .db import connect
from .loop_contracts import BudgetState, ResourceDecision
from .paths import db_paths


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
    idempotency_key: str = ""
    reserve: bool = True


@dataclass(frozen=True)
class ResourceGrant:
    decision: ResourceDecision | str
    reason: str
    budget_state: BudgetState
    retry_after_seconds: float = 0.0
    grant_id: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == ResourceDecision.ALLOW

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": str(self.decision),
            "reason": self.reason,
            "budget_state": self.budget_state.to_dict(),
            "retry_after_seconds": self.retry_after_seconds,
            "grant_id": self.grant_id,
        }


class ResourceLimitError(RuntimeError):
    """Raised when model admission is rejected with a structured grant."""

    def __init__(self, grant: ResourceGrant):
        self.grant = grant
        super().__init__(f"resource gateway {grant.decision}: {grant.reason}")


@dataclass(frozen=True)
class ResourceUsage:
    used_tokens: int = 0
    used_calls: int = 0
    used_cost: float = 0.0
    active: int = 0


class SQLiteResourceLedger:
    """Process-safe resource accounting keyed by a durable execution scope."""

    def __init__(self, home: Path):
        self.db_path = db_paths(home).resource_ledger
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS resource_scopes (
                    scope_id TEXT PRIMARY KEY,
                    token_budget INTEGER NOT NULL,
                    call_budget INTEGER NOT NULL,
                    cost_budget REAL NOT NULL,
                    qps_limit INTEGER NOT NULL,
                    max_concurrent INTEGER NOT NULL,
                    used_tokens INTEGER NOT NULL,
                    used_calls INTEGER NOT NULL,
                    used_cost REAL NOT NULL,
                    active INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS resource_grants (
                    id TEXT PRIMARY KEY,
                    scope_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    reserved_tokens INTEGER NOT NULL,
                    reserved_cost REAL NOT NULL,
                    units INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(scope_id, idempotency_key)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_resource_grants_scope_time "
                "ON resource_grants(scope_id, created_at)"
            )

    def ensure_scope(self, scope_id: str, limits: ResourceLimits) -> None:
        now = time.time()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO resource_scopes(
                    scope_id, token_budget, call_budget, cost_budget, qps_limit,
                    max_concurrent, used_tokens, used_calls, used_cost, active, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?)
                ON CONFLICT(scope_id) DO UPDATE SET
                    token_budget = excluded.token_budget,
                    call_budget = excluded.call_budget,
                    cost_budget = excluded.cost_budget,
                    qps_limit = excluded.qps_limit,
                    max_concurrent = excluded.max_concurrent,
                    updated_at = excluded.updated_at
                """,
                (
                    scope_id,
                    limits.token_budget,
                    limits.call_budget,
                    limits.cost_budget,
                    limits.qps_limit,
                    limits.max_concurrent,
                    now,
                ),
            )

    def usage(self, scope_id: str) -> ResourceUsage:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT used_tokens, used_calls, used_cost, active "
                "FROM resource_scopes WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()
        return ResourceUsage(*row) if row else ResourceUsage()

    def reserve(
        self,
        scope_id: str,
        limits: ResourceLimits,
        request: ResourceRequest,
        *,
        now: float,
    ) -> ResourceGrant:
        self.ensure_scope(scope_id, limits)
        grant_id = uuid.uuid4().hex
        idempotency_key = request.idempotency_key or grant_id
        try:
            with connect(self.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    """
                    SELECT id, status FROM resource_grants
                    WHERE scope_id = ? AND idempotency_key = ?
                    """,
                    (scope_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    usage = self._usage_in_transaction(conn, scope_id)
                    return _grant_from_usage(
                        limits,
                        ResourceDecision.ALLOW,
                        "idempotent_replay",
                        usage,
                        grant_id=str(existing[0]),
                    )
                usage = self._usage_in_transaction(conn, scope_id)
                decision, reason, retry_after = self._decision(
                    conn, scope_id, limits, request, usage, now=now
                )
                if decision != ResourceDecision.ALLOW:
                    return _grant_from_usage(
                        limits, decision, reason, usage, retry_after_seconds=retry_after
                    )
                conn.execute(
                    """
                    INSERT INTO resource_grants(
                        id, scope_id, idempotency_key, kind, reserved_tokens,
                        reserved_cost, units, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        grant_id,
                        scope_id,
                        idempotency_key,
                        request.kind,
                        max(0, request.estimated_tokens),
                        max(0.0, request.estimated_cost),
                        request.units,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE resource_scopes
                    SET used_tokens = used_tokens + ?, used_calls = used_calls + ?,
                        used_cost = used_cost + ?, active = active + ?, updated_at = ?
                    WHERE scope_id = ?
                    """,
                    (
                        max(0, request.estimated_tokens),
                        request.units,
                        max(0.0, request.estimated_cost),
                        request.units,
                        now,
                        scope_id,
                    ),
                )
                usage = self._usage_in_transaction(conn, scope_id)
                return _grant_from_usage(
                    limits, ResourceDecision.ALLOW, "ok", usage, grant_id=grant_id
                )
        except sqlite3.IntegrityError:
            usage = self.usage(scope_id)
            return _grant_from_usage(limits, ResourceDecision.ALLOW, "idempotent_replay", usage)

    def release(
        self,
        scope_id: str,
        grant_id: str,
        *,
        actual_tokens: int | None = None,
        actual_cost: float | None = None,
    ) -> ResourceUsage:
        if not grant_id:
            return self.usage(scope_id)
        now = time.time()
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT reserved_tokens, reserved_cost, units, status
                FROM resource_grants WHERE id = ? AND scope_id = ?
                """,
                (grant_id, scope_id),
            ).fetchone()
            if row is None or str(row[3]) != "active":
                return self._usage_in_transaction(conn, scope_id)
            reserved_tokens, reserved_cost, units = int(row[0]), float(row[1]), int(row[2])
            final_tokens = reserved_tokens if actual_tokens is None else max(0, actual_tokens)
            final_cost = reserved_cost if actual_cost is None else max(0.0, actual_cost)
            conn.execute(
                """
                UPDATE resource_scopes
                SET used_tokens = MAX(0, used_tokens + ?),
                    used_cost = MAX(0, used_cost + ?),
                    active = MAX(0, active - ?), updated_at = ?
                WHERE scope_id = ?
                """,
                (
                    final_tokens - reserved_tokens,
                    final_cost - reserved_cost,
                    units,
                    now,
                    scope_id,
                ),
            )
            conn.execute(
                "UPDATE resource_grants SET status = 'released', updated_at = ? WHERE id = ?",
                (now, grant_id),
            )
            return self._usage_in_transaction(conn, scope_id)

    def release_active_grants_for_scopes(self, scope_ids: Iterable[str]) -> list[str]:
        """Release reservations left by loop executions that no longer hold a lease.

        A reservation is only an in-flight concurrency claim.  Its token/cost
        reservation remains accounted for, while releasing it restores the
        concurrency slot so a crashed worker cannot permanently self-block a
        durable loop on its next recovery attempt.
        """
        scopes = tuple(sorted({str(scope_id) for scope_id in scope_ids if scope_id}))
        if not scopes:
            return []
        placeholders = ", ".join("?" for _ in scopes)
        now = time.time()
        released: list[str] = []
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT id, scope_id, units
                FROM resource_grants
                WHERE status = 'active' AND scope_id IN ({placeholders})
                """,
                scopes,
            ).fetchall()
            for grant_id, scope_id, units in rows:
                conn.execute(
                    """
                    UPDATE resource_scopes
                    SET active = MAX(0, active - ?), updated_at = ?
                    WHERE scope_id = ?
                    """,
                    (int(units), now, scope_id),
                )
                conn.execute(
                    "UPDATE resource_grants SET status = 'released', updated_at = ? WHERE id = ?",
                    (now, grant_id),
                )
                released.append(str(grant_id))
        return released

    @staticmethod
    def _usage_in_transaction(conn: sqlite3.Connection, scope_id: str) -> ResourceUsage:
        row = conn.execute(
            "SELECT used_tokens, used_calls, used_cost, active "
            "FROM resource_scopes WHERE scope_id = ?",
            (scope_id,),
        ).fetchone()
        return ResourceUsage(*row) if row else ResourceUsage()

    @staticmethod
    def _decision(
        conn: sqlite3.Connection,
        scope_id: str,
        limits: ResourceLimits,
        request: ResourceRequest,
        usage: ResourceUsage,
        *,
        now: float,
    ) -> tuple[ResourceDecision, str, float]:
        if request.units < 1:
            return ResourceDecision.BLOCK, "invalid_units", 0.0
        if limits.max_concurrent > 0 and usage.active + request.units > limits.max_concurrent:
            return ResourceDecision.PAUSE, "concurrency_limit", 0.0
        if limits.call_budget > 0 and usage.used_calls + request.units > limits.call_budget:
            return ResourceDecision.ESCALATE, "call_budget_exhausted", 0.0
        if (
            limits.token_budget > 0
            and usage.used_tokens + request.estimated_tokens > limits.token_budget
        ):
            return ResourceDecision.ESCALATE, "token_budget_exhausted", 0.0
        if limits.cost_budget > 0 and request.estimated_cost <= 0:
            return ResourceDecision.BLOCK, "cost_estimate_required", 0.0
        if limits.cost_budget > 0 and usage.used_cost + request.estimated_cost > limits.cost_budget:
            return ResourceDecision.ESCALATE, "cost_budget_exhausted", 0.0
        if limits.qps_limit > 0:
            row = conn.execute(
                """
                SELECT COUNT(*), MIN(created_at) FROM resource_grants
                WHERE scope_id = ? AND created_at > ?
                """,
                (scope_id, now - 1.0),
            ).fetchone()
            count = int(row[0]) if row else 0
            if count >= limits.qps_limit:
                earliest = float(row[1] or now)
                return ResourceDecision.PAUSE, "rate_limited", max(0.0, 1.0 - (now - earliest))
        return ResourceDecision.ALLOW, "ok", 0.0


class GlobalResourceGateway:
    """Deterministic resource guard for LLM/tool/subtask/API calls.

    This is a control-plane component, not a policy brain. It receives declared
    request costs and returns facts: allow, pause, escalate, or block.
    """

    def __init__(
        self,
        limits: ResourceLimits,
        *,
        ledger: SQLiteResourceLedger | None = None,
        scope_id: str = "",
    ):
        self.limits = limits
        self.ledger = ledger
        self.scope_id = scope_id
        self._used_tokens = 0
        self._used_calls = 0
        self._used_cost = 0.0
        self._active = 0
        self._call_times: deque[float] = deque()

    def inspect(self) -> BudgetState:
        if self.ledger is not None and self.scope_id:
            return self._budget_state_from_usage(
                ResourceDecision.ALLOW,
                "ok",
                self.ledger.usage(self.scope_id),
            )
        return BudgetState(
            decision=ResourceDecision.ALLOW,
            token_budget_remaining=_remaining(self.limits.token_budget, self._used_tokens),
            call_budget_remaining=_remaining(self.limits.call_budget, self._used_calls),
            cost_budget_remaining=_remaining_float(self.limits.cost_budget, self._used_cost),
            reason="ok",
        )

    def request(self, request: ResourceRequest, *, now: float | None = None) -> ResourceGrant:
        current = time.time() if now is None else now
        if not request.reserve:
            return ResourceGrant(
                decision=ResourceDecision.ALLOW,
                reason="admission_probe",
                budget_state=self.inspect(),
            )
        if self.ledger is not None and self.scope_id:
            return self.ledger.reserve(
                self.scope_id,
                self.limits,
                request,
                now=current,
            )
        if request.units < 1:
            return self._grant(ResourceDecision.BLOCK, "invalid_units")
        if (
            self.limits.max_concurrent > 0
            and self._active + request.units > self.limits.max_concurrent
        ):
            return self._grant(ResourceDecision.PAUSE, "concurrency_limit")
        if (
            self.limits.call_budget > 0
            and self._used_calls + request.units > self.limits.call_budget
        ):
            return self._grant(ResourceDecision.ESCALATE, "call_budget_exhausted")
        if (
            self.limits.token_budget > 0
            and self._used_tokens + request.estimated_tokens > self.limits.token_budget
        ):
            return self._grant(ResourceDecision.ESCALATE, "token_budget_exhausted")
        if (
            self.limits.cost_budget > 0
            and self._used_cost + request.estimated_cost > self.limits.cost_budget
        ):
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

    def release(
        self,
        *,
        units: int = 1,
        grant_id: str = "",
        actual_tokens: int | None = None,
        actual_cost: float | None = None,
    ) -> BudgetState:
        if self.ledger is not None and self.scope_id:
            usage = self.ledger.release(
                self.scope_id,
                grant_id,
                actual_tokens=actual_tokens,
                actual_cost=actual_cost,
            )
            return self._budget_state_from_usage(ResourceDecision.ALLOW, "ok", usage)
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

    def _budget_state_from_usage(
        self,
        decision: ResourceDecision,
        reason: str,
        usage: ResourceUsage,
    ) -> BudgetState:
        return BudgetState(
            decision=decision,
            token_budget_remaining=_remaining(self.limits.token_budget, usage.used_tokens),
            call_budget_remaining=_remaining(self.limits.call_budget, usage.used_calls),
            cost_budget_remaining=_remaining_float(self.limits.cost_budget, usage.used_cost),
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


def _grant_from_usage(
    limits: ResourceLimits,
    decision: ResourceDecision,
    reason: str,
    usage: ResourceUsage,
    *,
    retry_after_seconds: float = 0.0,
    grant_id: str = "",
) -> ResourceGrant:
    return ResourceGrant(
        decision=decision,
        reason=reason,
        budget_state=BudgetState(
            decision=decision,
            token_budget_remaining=_remaining(limits.token_budget, usage.used_tokens),
            call_budget_remaining=_remaining(limits.call_budget, usage.used_calls),
            cost_budget_remaining=_remaining_float(limits.cost_budget, usage.used_cost),
            reason=reason,
        ),
        retry_after_seconds=retry_after_seconds,
        grant_id=grant_id,
    )
