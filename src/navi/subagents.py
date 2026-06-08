from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect


@dataclass(frozen=True)
class SubagentRun:
    id: str
    role: str
    phase: str
    run_id: str
    status: str
    command: str
    input_json: str
    output_json: str
    error: str
    started_at: float
    updated_at: float
    completed_at: float


class SubagentRunStore:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = home / "subagents.db"
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subagent_runs (
                    id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    command TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    error TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_subagent_runs_role ON subagent_runs(role, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_subagent_runs_status ON subagent_runs(status, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_subagent_runs_run ON subagent_runs(run_id, updated_at)"
            )

    def start(
        self,
        *,
        role: str,
        phase: str,
        run_id: str = "",
        command: list[str] | None = None,
        input_data: dict[str, Any] | None = None,
    ) -> SubagentRun:
        now = time.time()
        run = SubagentRun(
            id=uuid.uuid4().hex,
            role=role,
            phase=phase,
            run_id=run_id,
            status="running",
            command=" ".join(command or ["navi", "subagent", role, phase, run_id]).strip(),
            input_json=json.dumps(input_data or {}, ensure_ascii=False, sort_keys=True),
            output_json="{}",
            error="",
            started_at=now,
            updated_at=now,
            completed_at=0.0,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO subagent_runs(
                    id, role, phase, run_id, status, command, input_json, output_json,
                    error, started_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.role,
                    run.phase,
                    run.run_id,
                    run.status,
                    run.command,
                    run.input_json,
                    run.output_json,
                    run.error,
                    run.started_at,
                    run.updated_at,
                    run.completed_at,
                ),
            )
        return run

    def finish(
        self,
        subagent_id: str,
        *,
        status: str,
        output_data: dict[str, Any] | None = None,
        error: str = "",
    ) -> SubagentRun | None:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError(f"unsupported subagent status: {status}")
        now = time.time()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE subagent_runs
                SET status = ?, output_json = ?, error = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(output_data or {}, ensure_ascii=False, sort_keys=True),
                    error,
                    now,
                    now,
                    subagent_id,
                ),
            )
        return self.get(subagent_id)

    def get(self, subagent_id: str) -> SubagentRun | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, role, phase, run_id, status, command, input_json, output_json,
                       error, started_at, updated_at, completed_at
                FROM subagent_runs WHERE id = ?
                """,
                (subagent_id,),
            ).fetchone()
        return SubagentRun(*row) if row else None

    def list(
        self,
        *,
        role: str = "",
        status: str = "",
        run_id: str = "",
        limit: int = 50,
    ) -> list[SubagentRun]:
        clauses: list[str] = []
        values: list[Any] = []
        if role:
            clauses.append("role = ?")
            values.append(role)
        if status:
            clauses.append("status = ?")
            values.append(status)
        if run_id:
            clauses.append("run_id = ?")
            values.append(run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, role, phase, run_id, status, command, input_json, output_json,
                       error, started_at, updated_at, completed_at
                FROM subagent_runs {where} ORDER BY updated_at DESC LIMIT ?
                """,
                values,
            ).fetchall()
        return [SubagentRun(*row) for row in rows]
