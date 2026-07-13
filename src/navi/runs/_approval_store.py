"""Approval persistence mixin for RunStore."""

from __future__ import annotations

import secrets
import time
import uuid
from pathlib import Path

from ..approval_contract import (
    APPROVAL_ACTION_SESSION_ELEVATION,
    APPROVAL_DECISION_APPROVE,
    APPROVAL_DECISION_REJECT,
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_EXPIRED,
    APPROVAL_STATUS_PENDING,
    APPROVAL_STATUS_REJECTED,
)
from ..db import connect
from ..schema import Column, Table
from .models import Approval


APPROVALS_TABLE = Table(
    "approvals",
    [
        Column("id", "TEXT", primary_key=True),
        Column("run_id", "TEXT", nullable=False),
        Column("action", "TEXT", nullable=False),
        Column("requested_tool", "TEXT", nullable=False),
        Column("requested_permission", "TEXT", nullable=False),
        Column("args_json", "TEXT", nullable=False),
        Column("source", "TEXT", nullable=False),
        Column("peer_id", "TEXT", nullable=False),
        Column("sender_id", "TEXT", nullable=False),
        Column("status", "TEXT", nullable=False),
        Column("code", "TEXT", nullable=False),
        Column("expires_at", "REAL", nullable=False),
        Column("created_at", "REAL", nullable=False),
        Column("updated_at", "REAL", nullable=False),
        Column("reason", "TEXT", nullable=False),
        Column("decision", "TEXT", nullable=False),
        Column("resolved_by", "TEXT", nullable=False),
    ],
)


class ApprovalStoreMixin:
    """Mixin providing approval persistence methods to RunStore."""

    db_path: Path

    def create_approval(
        self,
        *,
        run_id: str,
        action: str,
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
        requested_tool: str = "",
        requested_permission: str = "",
        args_json: str = "",
        reason: str = "",
        ttl_seconds: int = 900,
        code: str = "",
    ) -> Approval:
        now = time.time()
        expires_at = now + max(60, min(int(ttl_seconds or 900), 24 * 60 * 60))
        with connect(self.db_path) as conn:
            approval = Approval(
                id=uuid.uuid4().hex,
                run_id=run_id,
                action=action,
                requested_tool=requested_tool,
                requested_permission=requested_permission,
                args_json=args_json,
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
                status=APPROVAL_STATUS_PENDING,
                code=code or self._new_approval_code(conn),
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
                reason=reason,
                decision="",
                resolved_by="",
            )
            conn.execute(
                """
                INSERT INTO approvals(
                    id, run_id, action, requested_tool, requested_permission, args_json,
                    source, peer_id, sender_id, status, code,
                    expires_at, created_at, updated_at, reason, decision, resolved_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.id,
                    approval.run_id,
                    approval.action,
                    approval.requested_tool,
                    approval.requested_permission,
                    approval.args_json,
                    approval.source,
                    approval.peer_id,
                    approval.sender_id,
                    approval.status,
                    approval.code,
                    approval.expires_at,
                    approval.created_at,
                    approval.updated_at,
                    approval.reason,
                    approval.decision,
                    approval.resolved_by,
                ),
            )
        return approval

    def list_approvals(
        self,
        *,
        limit: int = 50,
        status: str = "",
        run_id: str = "",
    ) -> list[Approval]:
        self.expire_pending_approvals()
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, run_id, action, status, requested_tool, requested_permission, args_json,
                       source, peer_id, sender_id, code,
                       expires_at, created_at, updated_at, reason, decision, resolved_by
                FROM approvals{where}
                ORDER BY created_at DESC LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [self._approval_from_row(row) for row in rows]

    def pending_approval_for_run(
        self,
        run_id: str,
        *,
        sender_id: str = "",
        peer_id: str = "",
        source: str = "",
        action: str = "",
        requested_tool: str = "",
        requested_permission: str = "",
        args_json: str = "",
    ) -> Approval | None:
        self.expire_pending_approvals()
        clauses = ["run_id = ?", "status = ?"]
        params: list[object] = [run_id, APPROVAL_STATUS_PENDING]
        self._append_approval_filters(
            clauses,
            params,
            sender_id=sender_id,
            peer_id=peer_id,
            source=source,
            action=action,
            requested_tool=requested_tool,
            requested_permission=requested_permission,
            args_json=args_json,
        )
        return self._approval_where(clauses, params)

    def pending_approval_by_code(
        self,
        code: str,
        *,
        sender_id: str = "",
        peer_id: str = "",
        source: str = "",
    ) -> Approval | None:
        self.expire_pending_approvals()
        clauses = ["code = ?", "status = ?"]
        params: list[object] = [code, APPROVAL_STATUS_PENDING]
        self._append_approval_filters(
            clauses,
            params,
            sender_id=sender_id,
            peer_id=peer_id,
            source=source,
        )
        return self._approval_where(clauses, params)

    def approval_by_code(
        self,
        code: str,
        *,
        sender_id: str = "",
        peer_id: str = "",
        source: str = "",
    ) -> Approval | None:
        """Return the newest context-bound approval regardless of status.

        This is intentionally separate from ``pending_approval_by_code`` so
        repeated approve/reject commands can be handled idempotently without
        treating an already-resolved approval as missing.
        """
        self.expire_pending_approvals()
        clauses = ["code = ?"]
        params: list[object] = [code]
        self._append_approval_filters(
            clauses,
            params,
            sender_id=sender_id,
            peer_id=peer_id,
            source=source,
        )
        return self._approval_where(clauses, params)

    def approval_for_run(
        self,
        run_id: str,
        *,
        sender_id: str = "",
        peer_id: str = "",
        source: str = "",
    ) -> Approval | None:
        """Return the newest context-bound approval for a run in any status."""
        self.expire_pending_approvals()
        clauses = ["run_id = ?"]
        params: list[object] = [run_id]
        self._append_approval_filters(
            clauses,
            params,
            sender_id=sender_id,
            peer_id=peer_id,
            source=source,
        )
        return self._approval_where(clauses, params)

    def approved_approval_for_run(
        self,
        run_id: str,
        *,
        action: str = "",
        requested_tool: str = "",
        requested_permission: str = "",
        args_json: str = "",
    ) -> Approval | None:
        clauses = ["run_id = ?", "status = ?"]
        params: list[object] = [run_id, APPROVAL_STATUS_APPROVED]
        if action:
            clauses.append("action = ?")
            params.append(action)
        if requested_tool:
            clauses.append("requested_tool = ?")
            params.append(requested_tool)
        if requested_permission:
            clauses.append("requested_permission = ?")
            params.append(requested_permission)
        if args_json:
            clauses.append("args_json = ?")
            params.append(args_json)
        return self._approval_where(clauses, params)

    def active_session_elevation(
        self,
        *,
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
        now: float | None = None,
    ) -> Approval | None:
        threshold = time.time() if now is None else now
        clauses = ["action = ?", "status = ?", "expires_at >= ?"]
        params: list[object] = [
            APPROVAL_ACTION_SESSION_ELEVATION,
            APPROVAL_STATUS_APPROVED,
            threshold,
        ]
        self._append_approval_filters(
            clauses,
            params,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
        )
        return self._approval_where(clauses, params)

    def resolve_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        resolved_by: str = "",
    ) -> Approval | None:
        status = _status_for_decision(decision)
        if status is None:
            return None
        now = time.time()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE approvals
                SET status = ?, decision = ?, resolved_by = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    status,
                    decision,
                    resolved_by,
                    now,
                    approval_id,
                    APPROVAL_STATUS_PENDING,
                ),
            )
        return self.get_approval(approval_id)

    def resolve_approval_in_transaction(
        self,
        conn,
        approval_id: str,
        *,
        decision: str,
        resolved_by: str = "",
    ) -> Approval | None:
        status = _status_for_decision(decision)
        if status is None:
            return None
        now = time.time()
        conn.execute(
            """
            UPDATE approvals
            SET status = ?, decision = ?, resolved_by = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (
                status,
                decision,
                resolved_by,
                now,
                approval_id,
                APPROVAL_STATUS_PENDING,
            ),
        )
        return self._get_approval_with_connection(conn, approval_id)

    def get_approval(self, approval_id: str) -> Approval | None:
        with connect(self.db_path) as conn:
            row = self._select_approval_row(conn, "id = ?", [approval_id])
        return self._approval_from_row(row) if row else None

    def reject_pending_approvals_for_run(
        self,
        run_id: str,
        *,
        resolved_by: str = "system",
        decision: str = APPROVAL_DECISION_REJECT,
    ) -> int:
        now = time.time()
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = ?, decision = ?, resolved_by = ?, updated_at = ?
                WHERE run_id = ? AND status = ?
                """,
                (
                    APPROVAL_STATUS_REJECTED,
                    decision,
                    resolved_by,
                    now,
                    run_id,
                    APPROVAL_STATUS_PENDING,
                ),
            )
            return int(cursor.rowcount or 0)

    def expire_pending_approvals(self, *, now: float | None = None) -> int:
        threshold = time.time() if now is None else now
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = ?, updated_at = ?
                WHERE status = ? AND expires_at < ?
                """,
                (APPROVAL_STATUS_EXPIRED, threshold, APPROVAL_STATUS_PENDING, threshold),
            )
            return int(cursor.rowcount or 0)

    def _approval_where(self, clauses: list[str], params: list[object]) -> Approval | None:
        with connect(self.db_path) as conn:
            row = self._select_approval_row(conn, " AND ".join(clauses), params)
        return self._approval_from_row(row) if row else None

    @staticmethod
    def _select_approval_row(conn, where: str, params: list[object] | tuple[object, ...]):
        return conn.execute(
            f"""
            SELECT id, run_id, action, status, requested_tool, requested_permission, args_json,
                   source, peer_id, sender_id, code,
                   expires_at, created_at, updated_at, reason, decision, resolved_by
            FROM approvals
            WHERE {where}
            ORDER BY created_at DESC LIMIT 1
            """,
            params,
        ).fetchone()

    def _get_approval_with_connection(self, conn, approval_id: str) -> Approval | None:
        row = self._select_approval_row(conn, "id = ?", [approval_id])
        return self._approval_from_row(row) if row else None

    @staticmethod
    def _append_approval_filters(
        clauses: list[str],
        params: list[object],
        *,
        sender_id: str = "",
        peer_id: str = "",
        source: str = "",
        action: str = "",
        requested_tool: str = "",
        requested_permission: str = "",
        args_json: str = "",
    ) -> None:
        if sender_id:
            clauses.append("sender_id = ?")
            params.append(sender_id)
        if peer_id:
            clauses.append("peer_id = ?")
            params.append(peer_id)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if action:
            clauses.append("action = ?")
            params.append(action)
        if requested_tool:
            clauses.append("requested_tool = ?")
            params.append(requested_tool)
        if requested_permission:
            clauses.append("requested_permission = ?")
            params.append(requested_permission)
        if args_json:
            clauses.append("args_json = ?")
            params.append(args_json)

    @staticmethod
    def _approval_from_row(row: tuple) -> Approval:
        return Approval(*row)

    @staticmethod
    def _new_approval_code(conn) -> str:
        for _ in range(20):
            code = f"{secrets.randbelow(1_000_000):06d}"
            existing = conn.execute(
                "SELECT 1 FROM approvals WHERE code = ? AND status = ? LIMIT 1",
                (code, APPROVAL_STATUS_PENDING),
            ).fetchone()
            if existing is None:
                return code
        return f"{int(time.time() * 1000) % 1_000_000:06d}"


def _status_for_decision(decision: str) -> str | None:
    if decision == APPROVAL_DECISION_APPROVE:
        return APPROVAL_STATUS_APPROVED
    if decision == APPROVAL_DECISION_REJECT:
        return APPROVAL_STATUS_REJECTED
    return None
