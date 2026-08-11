"""RunStore: governed run/approval persistence."""

from __future__ import annotations

import json
import time
import typing
import uuid
from pathlib import Path
from typing import Any

from ..db import connect, check_schema_version, write_schema_version
from ..paths import db_paths
from ..persistence_scope import append_actor_scope
from ..schema import Column, Table, assert_schema_exact
from ._approval_store import APPROVALS_TABLE, ApprovalStoreMixin
from ._tool_call_log_store import (

    TOOL_CALL_LOGS_TABLE,
    ToolCallLogStoreMixin,
)
from .models import Run, _require_workspace

RUN_STORE_SCHEMA_VERSION = 4

RUNS_TABLE = Table(
    "runs",
    [
        Column("id", "TEXT", primary_key=True),
        Column("title", "TEXT", nullable=False),
        Column("phase", "TEXT", nullable=False),
        Column("governance", "TEXT", nullable=False),
        Column("acceptance", "TEXT", nullable=False),
        Column("resolution", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
        Column("updated_at", "REAL", nullable=False),
        Column("kind", "TEXT", nullable=False),
        Column("prompt", "TEXT", nullable=False),
        Column("source", "TEXT", nullable=False),
        Column("peer_id", "TEXT", nullable=False),
        Column("sender_id", "TEXT", nullable=False),
        Column("provider", "TEXT", nullable=False),
        Column("workspace", "TEXT", nullable=False),
        Column("autonomy_level", "TEXT", nullable=False),
        Column("trust_rule_id", "TEXT", nullable=False),
        Column("why_now", "TEXT", nullable=False),
        Column("plan_summary", "TEXT", nullable=False),
        Column("result_summary", "TEXT", nullable=False),
        Column("error", "TEXT", nullable=False),
    ],
)


class RunStore(ToolCallLogStoreMixin, ApprovalStoreMixin):
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = db_paths(home).runs
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            check_schema_version(conn, "runs", RUN_STORE_SCHEMA_VERSION)
            conn.execute(RUNS_TABLE.ddl)
            assert_schema_exact(conn, RUNS_TABLE)
            conn.execute(TOOL_CALL_LOGS_TABLE.ddl)
            assert_schema_exact(conn, TOOL_CALL_LOGS_TABLE)
            conn.execute(APPROVALS_TABLE.ddl)
            assert_schema_exact(conn, APPROVALS_TABLE)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_phase ON runs(phase, updated_at)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_actor_scope "
                "ON runs(source, peer_id, sender_id, workspace, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tool_call_logs_tool ON tool_call_logs(tool, started_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tool_call_logs_run ON tool_call_logs(run_id, started_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approvals_code ON approvals(code, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approvals_actor_scope "
                "ON approvals(source, peer_id, sender_id, status, created_at)"
            )
            write_schema_version(conn, "runs", RUN_STORE_SCHEMA_VERSION)

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
        phase: str = "pending",
        governance: str = "none",
        acceptance: str = "none",
        resolution: str = "none",
    ) -> Run:
        now = time.time()
        run = Run(
            id=uuid.uuid4().hex,
            title=title,
            phase=phase,
            governance=governance,
            acceptance=acceptance,
            resolution=resolution,
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
                    id, title, phase, governance, acceptance, resolution,
                    created_at, updated_at, kind, prompt, source,
                    peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id, why_now,
                    plan_summary, result_summary, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.title,
                    run.phase,
                    run.governance,
                    run.acceptance,
                    run.resolution,
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
                SELECT id, title, phase, governance, acceptance, resolution, created_at, updated_at, kind, prompt, source,
                       peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id,
                       why_now, plan_summary, result_summary, error
                FROM runs WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        return self._run_from_row(row) if row else None

    def list(self, *, limit: int = 50, offset: int = 0) -> typing.List[Run]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, title, phase, governance, acceptance, resolution, created_at, updated_at, kind, prompt, source,
                       peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id,
                       why_now, plan_summary, result_summary, error
                FROM runs ORDER BY updated_at DESC LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def list_by_phase(self, phase: str, *, limit: int = 20) -> typing.List[Run]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, title, phase, governance, acceptance, resolution, created_at, updated_at, kind, prompt, source,
                       peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id,
                       why_now, plan_summary, result_summary, error
                FROM runs WHERE phase = ? ORDER BY updated_at ASC LIMIT ?
                """,
                (phase, limit),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def list_by_phase_filtered(
        self,
        phase: str,
        *,
        source: str = "",
        kind: str = "",
        limit: int | None = None,
    ) -> typing.List[Run]:
        clauses = ["phase = ?"]
        params: list[Any] = [phase]
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
                SELECT id, title, phase, governance, acceptance, resolution, created_at, updated_at, kind, prompt, source,
                       peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id,
                       why_now, plan_summary, result_summary, error
                FROM runs WHERE {" AND ".join(clauses)} ORDER BY updated_at ASC{limit_clause}
                """,
                params,
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def count_runs(self, *, phase: str = "", source: str = "", kind: str = "") -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if phase:
            clauses.append("phase = ?")
            params.append(phase)
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

    def count_runs_by_phase(self) -> dict[str, int]:
        with connect(self.db_path) as conn:
            rows = conn.execute("SELECT phase, COUNT(*) FROM runs GROUP BY phase").fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def reconcile_completed_capability_approvals(self) -> typing.List[dict[str, Any]]:
        """Settle approved direct-call runs from their durable tool receipts.

        Current receipts carry the approval run id. The narrow legacy branch
        accepts only one unlinked, exact-argument receipt produced after the
        approval decision and before its original expiry, which lets upgrades
        repair runs completed before receipt linkage was introduced without
        guessing from domain state.
        """
        reconciled: typing.List[dict[str, Any]] = []
        with connect(self.db_path) as conn:
            candidates = conn.execute(
                """
                SELECT r.id, a.id, a.requested_tool, a.args_json,
                       a.updated_at, a.expires_at
                FROM runs r
                JOIN approvals a ON a.run_id = r.id
                WHERE r.kind = 'capability_approval'
                  AND r.phase IN ('pending', 'running')
                  AND a.status = 'approved'
                ORDER BY a.updated_at ASC
                """
            ).fetchall()
            for run_id, approval_id, tool, args_json, approved_at, expires_at in candidates:
                logs = conn.execute(
                    """
                    SELECT id, ok, error, run_id, facts_json
                    FROM tool_call_logs
                    WHERE tool = ?
                      AND started_at >= ? AND started_at <= ?
                      AND (
                          run_id = ?
                          OR (run_id = '' AND args_json = ?)
                      )
                    ORDER BY started_at ASC
                    """,
                    (tool, approved_at, expires_at, run_id, args_json),
                ).fetchall()
                settleable = []
                for row in logs:
                    try:
                        receipt_facts = json.loads(row[4])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(receipt_facts, dict):
                        continue
                    if receipt_facts.get("audit_phase") != "completed":
                        continue
                    if str(receipt_facts.get("loop_terminal_state") or "") in {
                        "paused",
                        "waiting_approval",
                    }:
                        continue
                    settleable.append(row)
                linked = [row for row in settleable if row[3] == run_id]
                legacy = [row for row in settleable if row[3] == ""]
                receipt = linked[-1] if linked else (legacy[0] if len(legacy) == 1 else None)
                if receipt is None:
                    continue
                log_id, ok, error, receipt_run_id, _facts_json = receipt
                if not receipt_run_id:
                    conn.execute(
                        "UPDATE tool_call_logs SET run_id = ? WHERE id = ? AND run_id = ''",
                        (run_id, log_id),
                    )
                conn.execute(
                    """
                    UPDATE runs
                    SET phase = 'ended', governance = 'approved',
                        acceptance = ?, resolution = ?, result_summary = ?,
                        error = ?, updated_at = ?
                    WHERE id = ? AND phase IN ('pending', 'running')
                    """,
                    (
                        "accepted" if ok else "rejected",
                        "success" if ok else "failed",
                        (
                            "approved capability receipt reconciled "
                            f"ok={str(bool(ok)).lower()} tool={tool} audit_log_id={log_id}"
                        ),
                        "" if ok else (error or "capability_failed"),
                        time.time(),
                        run_id,
                    ),
                )
                reconciled.append(
                    {
                        "run_id": run_id,
                        "approval_id": approval_id,
                        "audit_log_id": log_id,
                        "ok": bool(ok),
                        "legacy_receipt_linked": not bool(receipt_run_id),
                    }
                )
        return reconciled

    def list_by_phases(self, phases: typing.List[str], *, limit: int = 60) -> typing.List[Run]:
        if not phases:
            return []
        placeholders = ", ".join("?" for _ in phases)
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, title, phase, governance, acceptance, resolution, created_at, updated_at, kind, prompt, source,
                       peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id,
                       why_now, plan_summary, result_summary, error
                FROM runs WHERE phase IN ({placeholders}) ORDER BY updated_at ASC LIMIT ?
                """,
                [*phases, limit],
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def list_by_phases_scoped(
        self,
        phases: typing.List[str],
        *,
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
        workspace: str = "",
        kind: str = "",
        plan_summary: str = "",
        limit: int = 60,
    ) -> typing.List[Run]:
        if not phases:
            return []
        placeholders = ", ".join("?" for _ in phases)
        clauses = [f"phase IN ({placeholders})"]
        params: list[Any] = list(phases)
        append_actor_scope(
            clauses,
            params,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            workspace=workspace,
        )
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if plan_summary:
            clauses.append("plan_summary = ?")
            params.append(plan_summary)
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, title, phase, governance, acceptance, resolution, created_at, updated_at, kind, prompt, source,
                       peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id,
                       why_now, plan_summary, result_summary, error
                FROM runs WHERE {' AND '.join(clauses)} ORDER BY updated_at ASC LIMIT ?
                """,
                [*params, max(1, int(limit))],
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def count_by_phases_scoped(
        self,
        phases: typing.List[str],
        *,
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
        workspace: str = "",
    ) -> int:
        if not phases:
            return 0
        placeholders = ", ".join("?" for _ in phases)
        clauses = [f"phase IN ({placeholders})"]
        params: list[Any] = list(phases)
        append_actor_scope(
            clauses,
            params,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            workspace=workspace,
        )
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM runs WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()
        return int(row[0]) if row else 0

    def list_active_workspaces(self) -> set[str]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT workspace
                FROM runs
                WHERE phase IN (?, ?, ?) AND workspace != ''
                """,
                ("pending", "running", "paused"),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def delete_run(self, run_id: str) -> Run | None:
        run = self.get(run_id)
        if run is None:
            return None
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM execution_logs WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM approvals WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return run

    def update_run(
        self,
        run_id: str,
        *,
        phase: str | None = None,
        governance: str | None = None,
        acceptance: str | None = None,
        resolution: str | None = None,
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
            "phase": run.phase if phase is None else phase,
            "governance": run.governance if governance is None else governance,
            "acceptance": run.acceptance if acceptance is None else acceptance,
            "resolution": run.resolution if resolution is None else resolution,
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
                SET phase = ?, governance = ?, acceptance = ?, resolution = ?, plan_summary = ?, result_summary = ?, error = ?,
                    trust_rule_id = ?, autonomy_level = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    values["phase"],
                    values["governance"],
                    values["acceptance"],
                    values["resolution"],
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

    def update_run_in_transaction(
        self,
        conn,
        run_id: str,
        *,
        phase: str | None = None,
        governance: str | None = None,
        acceptance: str | None = None,
        resolution: str | None = None,
        plan_summary: str | None = None,
        result_summary: str | None = None,
        error: str | None = None,
        trust_rule_id: str | None = None,
        autonomy_level: str | None = None,
    ) -> Run | None:
        run = self._get_with_connection(conn, run_id)
        if run is None:
            return None
        values = {
            "phase": run.phase if phase is None else phase,
            "governance": run.governance if governance is None else governance,
            "acceptance": run.acceptance if acceptance is None else acceptance,
            "resolution": run.resolution if resolution is None else resolution,
            "plan_summary": run.plan_summary if plan_summary is None else plan_summary,
            "result_summary": run.result_summary if result_summary is None else result_summary,
            "error": run.error if error is None else error,
            "trust_rule_id": run.trust_rule_id if trust_rule_id is None else trust_rule_id,
            "autonomy_level": run.autonomy_level if autonomy_level is None else autonomy_level,
            "updated_at": time.time(),
        }
        conn.execute(
            """
            UPDATE runs
            SET phase = ?, governance = ?, acceptance = ?, resolution = ?, plan_summary = ?, result_summary = ?, error = ?,
                trust_rule_id = ?, autonomy_level = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                values["phase"],
                values["governance"],
                values["acceptance"],
                values["resolution"],
                values["plan_summary"],
                values["result_summary"],
                values["error"],
                values["trust_rule_id"],
                values["autonomy_level"],
                values["updated_at"],
                run_id,
            ),
        )
        return self._get_with_connection(conn, run_id)

    @staticmethod
    def _get_with_connection(conn, run_id: str) -> Run | None:
        row = conn.execute(
            """
            SELECT id, title, phase, governance, acceptance, resolution, created_at, updated_at, kind, prompt, source,
                   peer_id, sender_id, provider, workspace, autonomy_level, trust_rule_id,
                   why_now, plan_summary, result_summary, error
            FROM runs WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        return RunStore._run_from_row(row) if row else None

    # ------------------------------------------------------------- row mappers

    @staticmethod
    def _run_from_row(row: tuple) -> Run:
        return Run(*row)
