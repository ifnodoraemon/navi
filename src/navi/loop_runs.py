from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .db import check_schema_version, connect, write_schema_version
from .loop_contracts import LoopNode, LoopRunState, LoopSpec, LoopTerminalState
from .paths import db_paths
from .schema import Column, Table, assert_schema_exact


LOOP_RUN_STORE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class LoopCheckpoint:
    id: str
    run_id: str
    node: str
    inputs_json: str
    state_json: str
    created_at: float


@dataclass(frozen=True)
class LoopEvent:
    id: str
    run_id: str
    event_type: str
    node: str
    terminal_state: str
    checkpoint_id: str
    evidence_json: str
    created_at: float


class LoopRunStore:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = db_paths(home).loop_runs
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            check_schema_version(conn, "loop_runs", LOOP_RUN_STORE_SCHEMA_VERSION)
            conn.execute(LOOP_SPECS_TABLE.ddl)
            assert_schema_exact(conn, LOOP_SPECS_TABLE)
            conn.execute(LOOP_RUNS_TABLE.ddl)
            assert_schema_exact(conn, LOOP_RUNS_TABLE)
            conn.execute(LOOP_CHECKPOINTS_TABLE.ddl)
            assert_schema_exact(conn, LOOP_CHECKPOINTS_TABLE)
            conn.execute(LOOP_EVENTS_TABLE.ddl)
            assert_schema_exact(conn, LOOP_EVENTS_TABLE)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_loop_runs_goal ON loop_runs(goal_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_loop_runs_active ON loop_runs(terminal_state, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_loop_checkpoints_run ON loop_checkpoints(run_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_loop_events_run ON loop_events(run_id, created_at)"
            )
            write_schema_version(conn, "loop_runs", LOOP_RUN_STORE_SCHEMA_VERSION)

    def save_spec(self, spec: LoopSpec) -> None:
        spec.validate()
        with connect(self.db_path) as conn:
            self._save_spec_in_transaction(conn, spec)

    def get_spec_json(self, spec_id: str) -> str:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT spec_json FROM loop_specs WHERE id = ?",
                (spec_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"loop spec not found: {spec_id}")
        return str(row[0])

    def create_run(
        self,
        spec: LoopSpec,
        *,
        parent_run_id: str = "",
        node: LoopNode | str = LoopNode.PLAN,
        terminal_state: LoopTerminalState | str = "",
        evidence: dict[str, Any] | None = None,
        event_type: str = "loop.run_created",
    ) -> LoopRunState:
        spec.validate()
        now = time.time()
        state = LoopRunState(
            run_id=uuid.uuid4().hex,
            goal_id=spec.goal_id,
            loop_spec_id=spec.id,
            node=node,
            terminal_state=terminal_state,
            parent_run_id=parent_run_id,
            evidence=dict(evidence or {}),
            updated_at=now,
        )
        with connect(self.db_path) as conn:
            self._save_spec_in_transaction(conn, spec)
            conn.execute(
                """
                INSERT INTO loop_runs(
                    id, goal_id, loop_spec_id, node, terminal_state, checkpoint_id,
                    attempt, parent_run_id, child_run_ids_json, locked_resources_json,
                    evidence_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _loop_run_insert_values(state, created_at=now),
            )
            _insert_event(
                conn,
                state,
                event_type,
                evidence={"loop_spec_id": spec.id, **(evidence or {})},
            )
        return state

    def get_run(self, run_id: str) -> LoopRunState | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT {LOOP_RUNS_TABLE.select_list} FROM loop_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return _loop_run_from_row(row) if row else None

    def reopen_for_resume(self, run_id: str) -> LoopRunState:
        """Reopen a paused loop at PLAN after its external gate is resolved."""
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT {LOOP_RUNS_TABLE.select_list} FROM loop_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"loop run not found: {run_id}")
            current = _loop_run_from_row(row)
            if str(current.terminal_state) not in {
                str(LoopTerminalState.PAUSED),
                str(LoopTerminalState.WAITING_APPROVAL),
            }:
                raise ValueError(
                    f"loop run is not resumable from terminal state: {current.terminal_state}"
                )
            reopened = replace(
                current,
                node=LoopNode.EXECUTE,
                terminal_state="",
                updated_at=time.time(),
            )
            conn.execute(
                """
                UPDATE loop_runs
                SET node = ?, terminal_state = '', updated_at = ?
                WHERE id = ?
                """,
                (str(reopened.node), reopened.updated_at, run_id),
            )
            _insert_event(
                conn,
                reopened,
                "loop.resumed",
                evidence={"previous_terminal_state": str(current.terminal_state)},
            )
        return reopened

    def list_active(self, *, limit: int = 50) -> list[LoopRunState]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT {LOOP_RUNS_TABLE.select_list}
                FROM loop_runs
                WHERE terminal_state = ''
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_loop_run_from_row(row) for row in rows]

    def list_by_goal(self, goal_id: str, *, limit: int = 50) -> list[LoopRunState]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT {LOOP_RUNS_TABLE.select_list}
                FROM loop_runs
                WHERE goal_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (goal_id, limit),
            ).fetchall()
        return [_loop_run_from_row(row) for row in rows]

    def write_checkpoint(
        self,
        run_id: str,
        *,
        node: LoopNode | str,
        inputs: dict[str, Any],
        state: dict[str, Any],
        checkpoint_id: str = "",
    ) -> LoopCheckpoint:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"loop run not found: {run_id}")
        checkpoint = LoopCheckpoint(
            id=checkpoint_id or uuid.uuid4().hex,
            run_id=run_id,
            node=str(node),
            inputs_json=_json_dumps(inputs),
            state_json=_json_dumps(state),
            created_at=time.time(),
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO loop_checkpoints(id, run_id, node, inputs_json, state_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.id,
                    checkpoint.run_id,
                    checkpoint.node,
                    checkpoint.inputs_json,
                    checkpoint.state_json,
                    checkpoint.created_at,
                ),
            )
            _insert_event(
                conn,
                run,
                "loop.checkpoint",
                checkpoint_id=checkpoint.id,
                evidence={"node": str(node)},
            )
        return checkpoint

    def transition(
        self,
        run_id: str,
        *,
        node: LoopNode | str,
        checkpoint_id: str,
        terminal_state: LoopTerminalState | str = "",
        condition: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> LoopRunState:
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT {LOOP_RUNS_TABLE.select_list} FROM loop_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"loop run not found: {run_id}")
            checkpoint = conn.execute(
                "SELECT id FROM loop_checkpoints WHERE id = ? AND run_id = ?",
                (checkpoint_id, run_id),
            ).fetchone()
            if checkpoint is None:
                raise ValueError("LoopRun transition requires a persisted checkpoint")
            current = _loop_run_from_row(row)
            if current.is_terminal():
                raise ValueError("terminal LoopRunState cannot transition")
            target = str(terminal_state) if str(terminal_state).strip() else str(node)
            if not _transition_allowed(conn, current, target=target, condition=condition):
                raise ValueError("LoopRun transition is not allowed by LoopSpec.state_graph")
            next_state = current.transition(
                node=node,
                checkpoint_id=checkpoint_id,
                terminal_state=terminal_state,
                evidence=evidence,
            )
            conn.execute(
                """
                UPDATE loop_runs
                SET node = ?, terminal_state = ?, checkpoint_id = ?, attempt = ?,
                    parent_run_id = ?, child_run_ids_json = ?, locked_resources_json = ?,
                    evidence_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(next_state.node),
                    str(next_state.terminal_state),
                    next_state.checkpoint_id,
                    next_state.attempt,
                    next_state.parent_run_id,
                    _json_dumps(list(next_state.child_run_ids)),
                    _json_dumps(list(next_state.locked_resources)),
                    _json_dumps(next_state.evidence),
                    next_state.updated_at,
                    run_id,
                ),
            )
            _insert_event(
                conn,
                next_state,
                "loop.transition",
                checkpoint_id=checkpoint_id,
                evidence={**(evidence or {}), "condition": condition},
            )
        return next_state

    def list_checkpoints(self, run_id: str, *, limit: int = 100) -> list[LoopCheckpoint]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT {LOOP_CHECKPOINTS_TABLE.select_list}
                FROM loop_checkpoints
                WHERE run_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [LoopCheckpoint(*row) for row in rows]

    def list_events(self, run_id: str, *, limit: int = 100) -> list[LoopEvent]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT {LOOP_EVENTS_TABLE.select_list}
                FROM loop_events
                WHERE run_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [LoopEvent(*row) for row in rows]

    def _save_spec_in_transaction(self, conn: Any, spec: LoopSpec) -> None:
        conn.execute(
            """
            INSERT INTO loop_specs(id, goal_id, spec_json, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                goal_id = excluded.goal_id,
                spec_json = excluded.spec_json
            """,
            (spec.id, spec.goal_id, _json_dumps(spec.to_dict()), spec.created_at),
        )


def _insert_event(
    conn: Any,
    state: LoopRunState,
    event_type: str,
    *,
    checkpoint_id: str = "",
    evidence: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO loop_events(
            id, run_id, event_type, node, terminal_state, checkpoint_id, evidence_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex,
            state.run_id,
            event_type,
            str(state.node),
            str(state.terminal_state),
            checkpoint_id or state.checkpoint_id,
            _json_dumps(evidence or {}),
            time.time(),
        ),
    )


def _transition_allowed(
    conn: Any,
    state: LoopRunState,
    *,
    target: str,
    condition: str,
) -> bool:
    row = conn.execute(
        "SELECT spec_json FROM loop_specs WHERE id = ?",
        (state.loop_spec_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"loop spec not found: {state.loop_spec_id}")
    spec = _json_dict(row[0])
    for edge in spec.get("state_graph", []):
        if not isinstance(edge, dict):
            continue
        if edge.get("source") != str(state.node) or edge.get("target") != target:
            continue
        if condition and edge.get("condition") != condition:
            continue
        return True
    return False


def _loop_run_insert_values(state: LoopRunState, *, created_at: float) -> tuple[Any, ...]:
    return (
        state.run_id,
        state.goal_id,
        state.loop_spec_id,
        str(state.node),
        str(state.terminal_state),
        state.checkpoint_id,
        state.attempt,
        state.parent_run_id,
        _json_dumps(list(state.child_run_ids)),
        _json_dumps(list(state.locked_resources)),
        _json_dumps(state.evidence),
        created_at,
        state.updated_at,
    )


def _loop_run_from_row(row: Any) -> LoopRunState:
    return LoopRunState(
        run_id=str(row[0]),
        goal_id=str(row[1]),
        loop_spec_id=str(row[2]),
        node=str(row[3]),
        terminal_state=str(row[4]),
        checkpoint_id=str(row[5]),
        attempt=int(row[6]),
        parent_run_id=str(row[7]),
        child_run_ids=tuple(str(item) for item in _json_list(row[8])),
        locked_resources=tuple(str(item) for item in _json_list(row[9])),
        evidence=_json_dict(row[10]),
        updated_at=float(row[12]),
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


LOOP_SPECS_TABLE = Table(
    "loop_specs",
    [
        Column("id", "TEXT", primary_key=True),
        Column("goal_id", "TEXT", nullable=False),
        Column("spec_json", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
    ],
)

LOOP_RUNS_TABLE = Table(
    "loop_runs",
    [
        Column("id", "TEXT", primary_key=True),
        Column("goal_id", "TEXT", nullable=False),
        Column("loop_spec_id", "TEXT", nullable=False),
        Column("node", "TEXT", nullable=False),
        Column("terminal_state", "TEXT", nullable=False),
        Column("checkpoint_id", "TEXT", nullable=False),
        Column("attempt", "INTEGER", nullable=False),
        Column("parent_run_id", "TEXT", nullable=False),
        Column("child_run_ids_json", "TEXT", nullable=False),
        Column("locked_resources_json", "TEXT", nullable=False),
        Column("evidence_json", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
        Column("updated_at", "REAL", nullable=False),
    ],
)

LOOP_CHECKPOINTS_TABLE = Table(
    "loop_checkpoints",
    [
        Column("id", "TEXT", primary_key=True),
        Column("run_id", "TEXT", nullable=False),
        Column("node", "TEXT", nullable=False),
        Column("inputs_json", "TEXT", nullable=False),
        Column("state_json", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
    ],
)

LOOP_EVENTS_TABLE = Table(
    "loop_events",
    [
        Column("id", "TEXT", primary_key=True),
        Column("run_id", "TEXT", nullable=False),
        Column("event_type", "TEXT", nullable=False),
        Column("node", "TEXT", nullable=False),
        Column("terminal_state", "TEXT", nullable=False),
        Column("checkpoint_id", "TEXT", nullable=False),
        Column("evidence_json", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
    ],
)
