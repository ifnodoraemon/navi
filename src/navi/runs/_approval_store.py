"""Approval persistence mixin for RunStore."""

from __future__ import annotations

import secrets
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from ..db import connect
from ..schema import Column, Table
from .models import Approval, _approval_diagnostic_facts

if TYPE_CHECKING:
    pass

APPROVALS_TABLE = Table(
    "approvals",
    [
        Column("id", "TEXT", primary_key=True),
        Column("run_id", "TEXT", nullable=False),
        Column("code", "TEXT", nullable=False, unique=True),
        Column("action", "TEXT", nullable=False),
        Column("peer_id", "TEXT", nullable=False),
        Column("sender_id", "TEXT", nullable=False),
        Column("status", "TEXT", nullable=False),
        Column("expires_at", "REAL", nullable=False),
        Column("created_at", "REAL", nullable=False),
        Column("updated_at", "REAL", nullable=False),
    ],
)


class ApprovalStoreMixin:
    """Mixin providing approval persistence methods to RunStore.

    Requires:
    - db_path: Path (instance attribute, provided by RunStore.__init__)
    """

    db_path: Path

    def create_approval(
        self,
        *,
        run_id: str,
        peer_id: str,
        sender_id: str,
        action: str = "execute",
        ttl_seconds: int = 900,
    ) -> Approval:
        now = time.time()
        approval = Approval(
            id=uuid.uuid4().hex,
            run_id=run_id,
            code=self._new_code(),
            action=action,
            peer_id=peer_id,
            sender_id=sender_id,
            status="pending",
            expires_at=now + ttl_seconds,
            created_at=now,
            updated_at=now,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO approvals(
                    id, run_id, code, action, peer_id, sender_id, status,
                    expires_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.id,
                    approval.run_id,
                    approval.code,
                    approval.action,
                    approval.peer_id,
                    approval.sender_id,
                    approval.status,
                    approval.expires_at,
                    approval.created_at,
                    approval.updated_at,
                ),
            )
        return approval

    def reissue_approval(
        self,
        *,
        run_id: str,
        peer_id: str,
        sender_id: str,
        action: str = "execute",
        ttl_seconds: int = 900,
    ) -> Approval | None:
        """Mint a fresh approval for a run whose prior code expired, and pull the
        run back into awaiting_approval. The recovery path so an expired code is
        not a dead end — the user acts on the new code instead of re-creating the
        whole task.

        Concurrency: the check-then-mint-then-update sequence runs inside a
        single ``with connect()`` transaction (an IMMEDIATE write lock), so two
        surfaces racing to re-submit an expired code cannot both mint — the
        second sees the row the first inserted and returns it. Without this
        guard, each re-submission of an expired code mints yet another code —
        the observed "4-code storm" where the user receives four different codes
        for the same task within seconds.

        Returns the fresh (or existing pending) approval, or ``None`` when the
        run is no longer re-issuable because it was approved/advanced
        concurrently. Re-issue only applies to a run still in
        ``awaiting_approval``/``expired``; minting a code for a run that already
        moved to ``queued``/``running`` would both orphan the new code and risk
        pulling a live run back into awaiting_approval."""
        now = time.time()
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT id, run_id, code, action, peer_id, sender_id, status,
                           expires_at, created_at, updated_at
                    FROM approvals
                    WHERE run_id = ? AND sender_id = ? AND status = 'pending'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (run_id, sender_id),
                ).fetchone()
                if row is not None:
                    existing = Approval(*row)
                    if existing.expires_at >= now:
                        conn.execute("COMMIT")
                        return existing
                    conn.execute(
                        "UPDATE approvals SET status = ?, updated_at = ? "
                        "WHERE id = ? AND status = 'pending'",
                        ("expired", now, existing.id),
                    )
                # Only re-issue for a run still awaiting (or expired-out-of)
                # approval. If it advanced to queued/running/completed
                # concurrently, do not mint (it would orphan) or rewrite status.
                run_row = conn.execute(
                    "SELECT status FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                if run_row is None or run_row[0] not in (
                    "awaiting_approval",
                    "expired",
                ):
                    conn.execute("COMMIT")
                    return None
                approval = Approval(
                    id=uuid.uuid4().hex,
                    run_id=run_id,
                    code=self._new_code(),
                    action=action,
                    peer_id=peer_id,
                    sender_id=sender_id,
                    status="pending",
                    expires_at=now + ttl_seconds,
                    created_at=now,
                    updated_at=now,
                )
                conn.execute(
                    """
                    INSERT INTO approvals(
                        id, run_id, code, action, peer_id, sender_id, status,
                        expires_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval.id,
                        approval.run_id,
                        approval.code,
                        approval.action,
                        approval.peer_id,
                        approval.sender_id,
                        approval.status,
                        approval.expires_at,
                        approval.created_at,
                        approval.updated_at,
                    ),
                )
                conn.execute(
                    "UPDATE runs SET status = ?, updated_at = ? "
                    "WHERE id = ? AND status IN ('awaiting_approval', 'expired')",
                    ("awaiting_approval", now, run_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return approval

    def resolve_approval(
        self,
        code: str,
        sender_id: str,
        status: str,
        *,
        peer_id: str = "",
    ) -> Approval | None:
        approval = self.get_approval(code)
        if approval is None or approval.status != "pending":
            return None
        # FP-3: approval codes are channel-scoped. A code minted on one
        # peer must not be resolved from a different peer, even if the
        # sender id matches. When ``peer_id`` is supplied (the normal path
        # via ApprovalService), it must match the approval's peer.
        if peer_id and approval.peer_id and approval.peer_id != peer_id:
            return None
        if approval.sender_id != sender_id:
            return None
        now = time.time()
        new_status = "expired" if approval.expires_at < now else status
        # Concurrency guard: only transition while still pending. When two
        # surfaces (daemon, API, connector) race to resolve the same code, the
        # second UPDATE matches zero rows and get_approval reflects the status
        # the first writer set — a reject can no longer clobber an approve (or
        # vice versa) depending on commit order.
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE approvals SET status = ?, updated_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (new_status, now, approval.id),
            )
        return self.get_approval(code)

    def resolve_run_approval(self, run_id: str, *, sender_id: str, status: str) -> Approval | None:
        approval = self.pending_approval_for_run(run_id, sender_id=sender_id)
        if approval is None:
            return None
        now = time.time()
        new_status = "expired" if approval.expires_at < now else status
        # Concurrency guard: see resolve_approval — only transition pending rows.
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE approvals SET status = ?, updated_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (new_status, now, approval.id),
            )
        return self.get_approval(approval.code)

    def pending_approval_for_run(self, run_id: str, *, sender_id: str = "") -> Approval | None:
        now = time.time()
        with connect(self.db_path) as conn:
            if sender_id:
                row = conn.execute(
                    """
                    SELECT id, run_id, code, action, peer_id, sender_id, status,
                           expires_at, created_at, updated_at
                    FROM approvals
                    WHERE run_id = ? AND sender_id = ? AND status = 'pending'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (run_id, sender_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT id, run_id, code, action, peer_id, sender_id, status,
                           expires_at, created_at, updated_at
                    FROM approvals
                    WHERE run_id = ? AND status = 'pending'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
        approval = Approval(*row) if row else None
        if approval is None:
            return None
        if approval.expires_at < now:
            with connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE approvals SET status = ?, updated_at = ? "
                    "WHERE id = ? AND status = 'pending'",
                    ("expired", now, approval.id),
                )
            return None
        return approval

    def has_approved_execution(self, run_id: str) -> bool:
        return self.has_approved_action(run_id, "execute")

    def has_approved_action(self, run_id: str, action: str) -> bool:
        """True when this run has an approved approval for the given action.

        Used both for task-level execution grants (action='execute') and for
        per-capability sensitive-op grants (action='execute:<tool>'), so an
        approved sensitive op is not re-suspended on replay."""
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM approvals
                WHERE run_id = ? AND action = ? AND status = 'approved'
                LIMIT 1
                """,
                (run_id, action),
            ).fetchone()
        return row is not None

    def get_approval(self, code: str) -> Approval | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, run_id, code, action, peer_id, sender_id, status,
                       expires_at, created_at, updated_at
                FROM approvals WHERE code = ?
                """,
                (code,),
            ).fetchone()
        return Approval(*row) if row else None

    def approval_resolution_diagnostic(
        self, *, code: str = "", run_id: str = "", sender_id: str = ""
    ) -> dict:
        now = time.time()
        if code:
            approval = self.get_approval(code)
            if approval is None:
                return {"reason": "approval_code_not_found", "code_present": False}
            facts = _approval_diagnostic_facts(approval, now=now, sender_id=sender_id)
            if sender_id and approval.sender_id != sender_id:
                return facts | {"reason": "sender_mismatch"}
            if approval.status != "pending":
                return facts | {"reason": "approval_not_pending"}
            if approval.expires_at < now:
                return facts | {"reason": "approval_expired"}
            return facts | {"reason": "approval_pending"}
        if run_id:
            run = self.get(run_id)
            approvals = self._approvals_for_run(run_id)
            if run is None:
                return {
                    "reason": "run_not_found",
                    "run_id": run_id,
                    "approval_count": len(approvals),
                }
            if not approvals:
                return {
                    "reason": "run_has_no_approval",
                    "run_id": run_id,
                    "run_status": run.status,
                    "approval_count": 0,
                }
            pending = [approval for approval in approvals if approval.status == "pending"]
            if sender_id:
                sender_pending = [
                    approval for approval in pending if approval.sender_id == sender_id
                ]
                if sender_pending:
                    latest = sender_pending[0]
                    return _approval_diagnostic_facts(latest, now=now, sender_id=sender_id) | {
                        "reason": "approval_expired"
                        if latest.expires_at < now
                        else "approval_pending",
                    }
                if pending:
                    latest = pending[0]
                    return _approval_diagnostic_facts(latest, now=now, sender_id=sender_id) | {
                        "reason": "sender_mismatch",
                        "run_status": run.status,
                        "approval_count": len(approvals),
                    }
            latest = approvals[0]
            reason = (
                "approval_expired"
                if latest.status == "pending" and latest.expires_at < now
                else "approval_not_pending"
            )
            return _approval_diagnostic_facts(latest, now=now, sender_id=sender_id) | {
                "reason": reason,
                "run_status": run.status,
                "approval_count": len(approvals),
            }
        return {"reason": "approval_identifier_missing"}

    def list_approvals(self, *, limit: int = 50) -> list[Approval]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, code, action, peer_id, sender_id, status,
                       expires_at, created_at, updated_at
                FROM approvals ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [Approval(*row) for row in rows]

    def archive_expired_approvals(self, *, now: float | None = None) -> int:
        cutoff = time.time() if now is None else now
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = 'expired', updated_at = ?
                WHERE status = 'pending' AND expires_at < ?
                """,
                (cutoff, cutoff),
            )
            archived = int(cursor.rowcount or 0)
            # Move the owning runs out of awaiting_approval so they leave the
            # active list. They become `expired`: excluded from active views but
            # still re-issuable (a new code resurrects them) and deletable from
            # remote — closing the dead-end where a run could neither be approved
            # (code gone) nor removed (status not failed).
            conn.execute(
                """
                UPDATE runs
                SET status = 'expired', updated_at = ?
                WHERE status = 'awaiting_approval'
                  AND id IN (
                      SELECT run_id FROM approvals
                      WHERE status = 'expired'
                  )
                  AND id NOT IN (
                      SELECT run_id FROM approvals
                      WHERE status = 'pending' AND expires_at >= ?
                  )
                """,
                (cutoff, cutoff),
            )
            return archived

    def _approvals_for_run(self, run_id: str) -> list[Approval]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, code, action, peer_id, sender_id, status,
                       expires_at, created_at, updated_at
                FROM approvals WHERE run_id = ?
                ORDER BY created_at DESC
                """,
                (run_id,),
            ).fetchall()
        return [Approval(*row) for row in rows]

    def latest_approval_for_run(self, run_id: str) -> Approval | None:
        """Most recent approval (any status) for a run. Used by the re-issue path
        to find an expired code to replace when there is no pending one left."""
        approvals = self._approvals_for_run(run_id)
        return approvals[0] if approvals else None

    @staticmethod
    def _new_code() -> str:
        return f"{secrets.randbelow(1_000_000):06d}"
