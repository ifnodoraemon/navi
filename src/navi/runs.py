from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect, ensure_schema_version


RUN_STORE_SCHEMA_VERSION = 2


def _require_workspace(workspace: str) -> str:
    value = workspace.strip()
    if not value:
        raise ValueError("workspace is required")
    return value


@dataclass(frozen=True)
class Run:
    id: str
    title: str
    status: str
    created_at: float
    updated_at: float
    kind: str = "manual"
    prompt: str = ""
    source: str = "local"
    peer_id: str = ""
    sender_id: str = ""
    provider: str = ""
    workspace: str = ""
    autonomy_level: str = "L2"
    trust_rule_id: str = ""
    why_now: str = ""
    plan_summary: str = ""
    result_summary: str = ""
    error: str = ""


@dataclass(frozen=True)
class Approval:
    id: str
    run_id: str
    code: str
    action: str
    peer_id: str
    sender_id: str
    status: str
    expires_at: float
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class Watch:
    id: str
    cron: str
    prompt: str
    peer_id: str
    sender_id: str
    enabled: bool
    next_run_at: float
    last_run_at: float
    created_at: float
    updated_at: float
    workspace: str = ""
    kind: str = "recurring"


@dataclass(frozen=True)
class ExecutionLog:
    id: str
    run_id: str
    provider: str
    phase: str
    command: str
    stdout: str
    stderr: str
    exit_code: int
    started_at: float
    ended_at: float


@dataclass(frozen=True)
class ToolCallLog:
    id: str
    tool: str
    args_json: str
    ok: bool
    facts_json: str
    error: str
    started_at: float
    ended_at: float
    run_id: str = ""
    trace_id: str = ""


class RunStore:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = home / "runs.db"
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            ensure_schema_version(conn, "runs", RUN_STORE_SCHEMA_VERSION)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    kind TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    source TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    autonomy_level TEXT NOT NULL,
                    trust_rule_id TEXT NOT NULL,
                    why_now TEXT NOT NULL,
                    plan_summary TEXT NOT NULL,
                    result_summary TEXT NOT NULL,
                    error TEXT NOT NULL
                )
                """
            )
            _assert_schema_exact(conn, "runs", _RUN_SCHEMA)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    code TEXT NOT NULL UNIQUE,
                    action TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            _assert_schema_exact(conn, "approvals", _APPROVAL_SCHEMA)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    id TEXT PRIMARY KEY,
                    cron TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    next_run_at REAL NOT NULL,
                    last_run_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    workspace TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'recurring'
                )
                """
            )
            _assert_schema_exact(conn, "watches", _WATCH_SCHEMA)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_logs (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    command TEXT NOT NULL,
                    stdout TEXT NOT NULL,
                    stderr TEXT NOT NULL,
                    exit_code INTEGER NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL NOT NULL
                )
                """
            )
            _assert_schema_exact(conn, "execution_logs", _EXECUTION_LOG_SCHEMA)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_call_logs (
                    id TEXT PRIMARY KEY,
                    tool TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    facts_json TEXT NOT NULL,
                    error TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    trace_id TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._migrate_tool_call_logs(conn)
            _assert_schema_exact(conn, "tool_call_logs", _TOOL_CALL_LOG_SCHEMA)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_approvals_code ON approvals(code)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_watches_next ON watches(enabled, next_run_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tool_call_logs_tool ON tool_call_logs(tool, started_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tool_call_logs_run ON tool_call_logs(run_id, started_at)"
            )

    @staticmethod
    def _migrate_tool_call_logs(conn) -> None:
        """Backfill run_id/trace_id columns on pre-v2 runs.db installs.

        Principle 1.2: schema drift is rejected loudly by _assert_schema_exact,
        so migration must bring the on-disk shape to the current contract before
        the assertion runs."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tool_call_logs)")}
        if "run_id" not in columns:
            conn.execute("ALTER TABLE tool_call_logs ADD COLUMN run_id TEXT NOT NULL DEFAULT ''")
        if "trace_id" not in columns:
            conn.execute("ALTER TABLE tool_call_logs ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''")

    def create(
        self,
        title: str,
        *,
        kind: str = "manual",
        prompt: str = "",
        source: str = "local",
        peer_id: str = "",
        sender_id: str = "",
        provider: str = "",
        workspace: str,
        autonomy_level: str = "L2",
        trust_rule_id: str = "",
        why_now: str = "",
        status: str = "pending",
    ) -> Run:
        now = time.time()
        run = Run(
            id=uuid.uuid4().hex,
            title=title,
            status=status,
            created_at=now,
            updated_at=now,
            kind=kind,
            prompt=prompt or title,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            provider=provider,
            workspace=_require_workspace(workspace),
            autonomy_level=autonomy_level,
            trust_rule_id=trust_rule_id,
            why_now=why_now,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO runs(
                    id, title, status, created_at, updated_at, kind, prompt, source,
                    peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id, why_now,
                    plan_summary, result_summary, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.title,
                    run.status,
                    run.created_at,
                    run.updated_at,
                    run.kind,
                    run.prompt,
                    run.source,
                    run.peer_id,
                    run.sender_id,
                    run.provider,
                    run.workspace,
                    run.autonomy_level,
                    run.trust_rule_id,
                    run.why_now,
                    run.plan_summary,
                    run.result_summary,
                    run.error,
                ),
            )
        return run

    def get(self, run_id: str) -> Run | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, title, status, created_at, updated_at, kind, prompt, source,
                       peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id,
                       why_now, plan_summary, result_summary, error
                FROM runs WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        return self._run_from_row(row) if row else None

    def list(self, *, limit: int = 50) -> list[Run]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, title, status, created_at, updated_at, kind, prompt, source,
                       peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id,
                       why_now, plan_summary, result_summary, error
                FROM runs ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def list_by_status(self, status: str, *, limit: int = 20) -> list[Run]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, title, status, created_at, updated_at, kind, prompt, source,
                       peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id,
                       why_now, plan_summary, result_summary, error
                FROM runs WHERE status = ? ORDER BY updated_at ASC LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def list_by_status_filtered(
        self,
        status: str,
        *,
        source: str = "",
        kind: str = "",
        limit: int | None = None,
    ) -> list[Run]:
        clauses = ["status = ?"]
        params: list[Any] = [status]
        if source:
            clauses.append("source = ?")
            params.append(source)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        limit_clause = ""
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(limit)
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, title, status, created_at, updated_at, kind, prompt, source,
                       peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id,
                       why_now, plan_summary, result_summary, error
                FROM runs WHERE {" AND ".join(clauses)} ORDER BY updated_at ASC{limit_clause}
                """,
                params,
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def count_runs(self, *, status: str = "", source: str = "", kind: str = "") -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with connect(self.db_path) as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM runs{where}", params).fetchone()
        return int(row[0] if row else 0)

    def count_runs_by_status(self) -> dict[str, int]:
        with connect(self.db_path) as conn:
            rows = conn.execute("SELECT status, COUNT(*) FROM runs GROUP BY status").fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def list_by_statuses(self, statuses: list[str], *, limit: int = 60) -> list[Run]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, title, status, created_at, updated_at, kind, prompt, source,
                       peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id,
                       why_now, plan_summary, result_summary, error
                FROM runs WHERE status IN ({placeholders}) ORDER BY updated_at ASC LIMIT ?
                """,
                [*statuses, limit],
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def update_status(self, run_id: str, status: str) -> Run | None:
        return self.update_run(run_id, status=status)

    def delete_run(self, run_id: str) -> Run | None:
        run = self.get(run_id)
        if run is None:
            return None
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM approvals WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM execution_logs WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return run

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        plan_summary: str | None = None,
        result_summary: str | None = None,
        error: str | None = None,
        trust_rule_id: str | None = None,
        autonomy_level: str | None = None,
    ) -> Run | None:
        run = self.get(run_id)
        if run is None:
            return None
        values = {
            "status": run.status if status is None else status,
            "plan_summary": run.plan_summary if plan_summary is None else plan_summary,
            "result_summary": run.result_summary if result_summary is None else result_summary,
            "error": run.error if error is None else error,
            "trust_rule_id": run.trust_rule_id if trust_rule_id is None else trust_rule_id,
            "autonomy_level": run.autonomy_level if autonomy_level is None else autonomy_level,
            "updated_at": time.time(),
        }
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, plan_summary = ?, result_summary = ?, error = ?,
                    trust_rule_id = ?, autonomy_level = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    values["status"],
                    values["plan_summary"],
                    values["result_summary"],
                    values["error"],
                    values["trust_rule_id"],
                    values["autonomy_level"],
                    values["updated_at"],
                    run_id,
                ),
            )
        return self.get(run_id)

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
    ) -> Approval:
        """Mint a fresh approval for a run whose prior code expired, and pull the
        run back into awaiting_approval. The recovery path so an expired code is
        not a dead end — the user acts on the new code instead of re-creating the
        whole task."""
        approval = self.create_approval(
            run_id=run_id,
            peer_id=peer_id,
            sender_id=sender_id,
            action=action,
            ttl_seconds=ttl_seconds,
        )
        self.update_run(run_id, status="awaiting_approval")
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
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE approvals SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, now, approval.id),
            )
        return self.get_approval(code)

    def resolve_run_approval(self, run_id: str, *, sender_id: str, status: str) -> Approval | None:
        approval = self.pending_approval_for_run(run_id, sender_id=sender_id)
        if approval is None:
            return None
        now = time.time()
        new_status = "expired" if approval.expires_at < now else status
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE approvals SET status = ?, updated_at = ? WHERE id = ?",
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
                    "UPDATE approvals SET status = ?, updated_at = ? WHERE id = ?",
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

    def create_watch(
        self,
        *,
        cron: str,
        prompt: str,
        peer_id: str,
        sender_id: str,
        next_run_at: float,
        workspace: str,
        kind: str = "recurring",
    ) -> Watch:
        now = time.time()
        watch = Watch(
            id=uuid.uuid4().hex,
            cron=cron,
            prompt=prompt,
            peer_id=peer_id,
            sender_id=sender_id,
            enabled=True,
            next_run_at=next_run_at,
            last_run_at=0.0,
            created_at=now,
            updated_at=now,
            workspace=_require_workspace(workspace),
            kind=kind,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO watches(
                    id, cron, prompt, peer_id, sender_id, enabled,
                    next_run_at, last_run_at, created_at, updated_at, workspace, kind
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    watch.id,
                    watch.cron,
                    watch.prompt,
                    watch.peer_id,
                    watch.sender_id,
                    int(watch.enabled),
                    watch.next_run_at,
                    watch.last_run_at,
                    watch.created_at,
                    watch.updated_at,
                    watch.workspace,
                    watch.kind,
                ),
            )
        return watch

    def list_watches(self, *, limit: int = 50) -> list[Watch]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, cron, prompt, peer_id, sender_id, enabled,
                       next_run_at, last_run_at, created_at, updated_at, workspace, kind
                FROM watches ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._watch_from_row(row) for row in rows]

    def get_watch(self, watch_id: str) -> Watch | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, cron, prompt, peer_id, sender_id, enabled,
                       next_run_at, last_run_at, created_at, updated_at, workspace, kind
                FROM watches WHERE id = ?
                """,
                (watch_id,),
            ).fetchone()
        return self._watch_from_row(row) if row else None

    def due_watches(self, now: float) -> list[Watch]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, cron, prompt, peer_id, sender_id, enabled,
                       next_run_at, last_run_at, created_at, updated_at, workspace, kind
                FROM watches WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at ASC
                """,
                (now,),
            ).fetchall()
        return [self._watch_from_row(row) for row in rows]

    def mark_watch_run(
        self, watch_id: str, *, last_run_at: float, next_run_at: float
    ) -> Watch | None:
        now = time.time()
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE watches SET last_run_at = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
                (last_run_at, next_run_at, now, watch_id),
            )
        return self.get_watch(watch_id)

    def mark_watch_completed_once(self, watch_id: str, *, last_run_at: float) -> Watch | None:
        now = time.time()
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE watches SET enabled = 0, last_run_at = ?, updated_at = ? WHERE id = ?",
                (last_run_at, now, watch_id),
            )
        return self.get_watch(watch_id)

    def delete_watch(self, watch_id: str) -> Watch | None:
        watch = self.get_watch(watch_id)
        if watch is None:
            return None
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        return watch

    def add_execution_log(
        self,
        *,
        run_id: str,
        provider: str,
        phase: str,
        command: str,
        stdout: str,
        stderr: str,
        exit_code: int,
        started_at: float,
        ended_at: float,
    ) -> ExecutionLog:
        log = ExecutionLog(
            id=uuid.uuid4().hex,
            run_id=run_id,
            provider=provider,
            phase=phase,
            command=command,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            started_at=started_at,
            ended_at=ended_at,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO execution_logs(
                    id, run_id, provider, phase, command, stdout, stderr,
                    exit_code, started_at, ended_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log.id,
                    log.run_id,
                    log.provider,
                    log.phase,
                    log.command,
                    log.stdout,
                    log.stderr,
                    log.exit_code,
                    log.started_at,
                    log.ended_at,
                ),
            )
        return log

    def add_tool_call_log(
        self,
        *,
        tool: str,
        args_json: str,
        ok: bool,
        facts_json: str,
        error: str,
        started_at: float,
        ended_at: float,
        run_id: str = "",
        trace_id: str = "",
    ) -> ToolCallLog:
        log = ToolCallLog(
            id=uuid.uuid4().hex,
            tool=tool,
            args_json=args_json,
            ok=ok,
            facts_json=facts_json,
            error=error,
            started_at=started_at,
            ended_at=ended_at,
            run_id=run_id,
            trace_id=trace_id,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO tool_call_logs(
                    id, tool, args_json, ok, facts_json, error, started_at, ended_at,
                    run_id, trace_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log.id,
                    log.tool,
                    log.args_json,
                    int(log.ok),
                    log.facts_json,
                    log.error,
                    log.started_at,
                    log.ended_at,
                    log.run_id,
                    log.trace_id,
                ),
            )
        return log

    def list_execution_logs(
        self, run_id: str | None = None, *, limit: int = 50
    ) -> list[ExecutionLog]:
        with connect(self.db_path) as conn:
            if run_id:
                rows = conn.execute(
                    """
                    SELECT id, run_id, provider, phase, command, stdout, stderr,
                           exit_code, started_at, ended_at
                    FROM execution_logs WHERE run_id = ? ORDER BY started_at DESC LIMIT ?
                    """,
                    (run_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, run_id, provider, phase, command, stdout, stderr,
                           exit_code, started_at, ended_at
                    FROM execution_logs ORDER BY started_at DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [ExecutionLog(*row) for row in rows]

    def list_tool_call_logs(self, *, limit: int = 50) -> list[ToolCallLog]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, tool, args_json, ok, facts_json, error, started_at, ended_at,
                       run_id, trace_id
                FROM tool_call_logs ORDER BY started_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._tool_call_log_from_row(row) for row in rows]

    def list_tool_call_logs_for_run(self, run_id: str, *, limit: int = 200) -> list[ToolCallLog]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, tool, args_json, ok, facts_json, error, started_at, ended_at,
                       run_id, trace_id
                FROM tool_call_logs WHERE run_id = ? ORDER BY started_at ASC LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [self._tool_call_log_from_row(row) for row in rows]

    @staticmethod
    def _run_from_row(row: tuple) -> Run:
        return Run(*row)

    @staticmethod
    def _watch_from_row(row: tuple) -> Watch:
        values = list(row)
        values[5] = bool(values[5])
        return Watch(*values)

    @staticmethod
    def _tool_call_log_from_row(row: tuple) -> ToolCallLog:
        values = list(row)
        values[3] = bool(values[3])
        return ToolCallLog(*values)

    @staticmethod
    def _new_code() -> str:
        return f"{secrets.randbelow(1_000_000):06d}"


_RUN_SCHEMA = [
    ("id", "TEXT", 0, 1),
    ("title", "TEXT", 1, 0),
    ("status", "TEXT", 1, 0),
    ("created_at", "REAL", 1, 0),
    ("updated_at", "REAL", 1, 0),
    ("kind", "TEXT", 1, 0),
    ("prompt", "TEXT", 1, 0),
    ("source", "TEXT", 1, 0),
    ("peer_id", "TEXT", 1, 0),
    ("sender_id", "TEXT", 1, 0),
    ("provider", "TEXT", 1, 0),
    ("workspace", "TEXT", 1, 0),
    ("autonomy_level", "TEXT", 1, 0),
    ("trust_rule_id", "TEXT", 1, 0),
    ("why_now", "TEXT", 1, 0),
    ("plan_summary", "TEXT", 1, 0),
    ("result_summary", "TEXT", 1, 0),
    ("error", "TEXT", 1, 0),
]

_APPROVAL_SCHEMA = [
    ("id", "TEXT", 0, 1),
    ("run_id", "TEXT", 1, 0),
    ("code", "TEXT", 1, 0),
    ("action", "TEXT", 1, 0),
    ("peer_id", "TEXT", 1, 0),
    ("sender_id", "TEXT", 1, 0),
    ("status", "TEXT", 1, 0),
    ("expires_at", "REAL", 1, 0),
    ("created_at", "REAL", 1, 0),
    ("updated_at", "REAL", 1, 0),
]

_WATCH_SCHEMA = [
    ("id", "TEXT", 0, 1),
    ("cron", "TEXT", 1, 0),
    ("prompt", "TEXT", 1, 0),
    ("peer_id", "TEXT", 1, 0),
    ("sender_id", "TEXT", 1, 0),
    ("enabled", "INTEGER", 1, 0),
    ("next_run_at", "REAL", 1, 0),
    ("last_run_at", "REAL", 1, 0),
    ("created_at", "REAL", 1, 0),
    ("updated_at", "REAL", 1, 0),
    ("workspace", "TEXT", 1, 0),
    ("kind", "TEXT", 1, 0),
]

_EXECUTION_LOG_SCHEMA = [
    ("id", "TEXT", 0, 1),
    ("run_id", "TEXT", 1, 0),
    ("provider", "TEXT", 1, 0),
    ("phase", "TEXT", 1, 0),
    ("command", "TEXT", 1, 0),
    ("stdout", "TEXT", 1, 0),
    ("stderr", "TEXT", 1, 0),
    ("exit_code", "INTEGER", 1, 0),
    ("started_at", "REAL", 1, 0),
    ("ended_at", "REAL", 1, 0),
]

_TOOL_CALL_LOG_SCHEMA = [
    ("id", "TEXT", 0, 1),
    ("tool", "TEXT", 1, 0),
    ("args_json", "TEXT", 1, 0),
    ("ok", "INTEGER", 1, 0),
    ("facts_json", "TEXT", 1, 0),
    ("error", "TEXT", 1, 0),
    ("started_at", "REAL", 1, 0),
    ("ended_at", "REAL", 1, 0),
    ("run_id", "TEXT", 1, 0),
    ("trace_id", "TEXT", 1, 0),
]


def _assert_schema_exact(conn, table: str, expected: list[tuple[str, str, int, int]]) -> None:
    schema = [
        (row[1], str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    ]
    if schema != expected:
        raise RuntimeError(f"{table} schema mismatch; expected current Navi schema")


def _approval_diagnostic_facts(approval: Approval, *, now: float, sender_id: str = "") -> dict:
    return {
        "approval_id": approval.id,
        "run_id": approval.run_id,
        "code_present": bool(approval.code),
        "action": approval.action,
        "status": approval.status,
        "sender_matches": not sender_id or approval.sender_id == sender_id,
        "is_expired": approval.expires_at < now,
        "expires_at": approval.expires_at,
        "updated_at": approval.updated_at,
    }
