"""Execution log persistence mixin for RunStore."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from ..db import connect
from ..schema import Column, Table
from .models import ExecutionLog, ToolCallLog

if TYPE_CHECKING:
    pass

EXECUTION_LOGS_TABLE = Table(
    "execution_logs",
    [
        Column("id", "TEXT", primary_key=True),
        Column("run_id", "TEXT", nullable=False),
        Column("provider", "TEXT", nullable=False),
        Column("phase", "TEXT", nullable=False),
        Column("command", "TEXT", nullable=False),
        Column("stdout", "TEXT", nullable=False),
        Column("stderr", "TEXT", nullable=False),
        Column("exit_code", "INTEGER", nullable=False),
        Column("started_at", "REAL", nullable=False),
        Column("ended_at", "REAL", nullable=False),
    ],
)
TOOL_CALL_LOGS_TABLE = Table(
    "tool_call_logs",
    [
        Column("id", "TEXT", primary_key=True),
        Column("tool", "TEXT", nullable=False),
        Column("args_json", "TEXT", nullable=False),
        Column("ok", "INTEGER", nullable=False),
        Column("facts_json", "TEXT", nullable=False),
        Column("error", "TEXT", nullable=False),
        Column("started_at", "REAL", nullable=False),
        Column("ended_at", "REAL", nullable=False),
        Column("run_id", "TEXT", nullable=False, default="''"),
        Column("trace_id", "TEXT", nullable=False, default="''"),
    ],
)


class ExecutionLogStoreMixin:
    """Mixin providing execution log persistence methods to RunStore.

    Requires:
    - db_path: Path (instance attribute, provided by RunStore.__init__)
    """

    db_path: Path

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
    def _tool_call_log_from_row(row: tuple) -> ToolCallLog:
        values = list(row)
        values[3] = bool(values[3])
        return ToolCallLog(*values)
