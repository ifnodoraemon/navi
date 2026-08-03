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


LOOP_RUN_STORE_SCHEMA_VERSION = 3
DETACHED_EXECUTION_RECOVERY_GRACE_SECONDS = 90.0


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
            _migrate_loop_runs_v3(conn)
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
                "CREATE INDEX IF NOT EXISTS idx_loop_runs_lease "
                "ON loop_runs(terminal_state, lease_expires_at, updated_at)"
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
                    evidence_json, created_at, updated_at, version, lease_owner,
                    lease_expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def claim_for_execution(
        self,
        run_id: str,
        *,
        owner: str,
        lease_seconds: float = 180.0,
        now: float | None = None,
        allow_paused: bool = False,
    ) -> LoopRunState | None:
        """Atomically claim one loop for a single execution driver."""
        current_time = time.time() if now is None else now
        terminal_clause = (
            "terminal_state IN ('', 'paused')" if allow_paused else "terminal_state = ''"
        )
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                f"""
                UPDATE loop_runs
                SET lease_owner = ?, lease_expires_at = ?, version = version + 1,
                    updated_at = ?
                WHERE id = ? AND {terminal_clause}
                  AND (lease_owner = '' OR lease_owner = ? OR lease_expires_at <= ?)
                """,
                (
                    owner,
                    current_time + max(1.0, lease_seconds),
                    current_time,
                    run_id,
                    owner,
                    current_time,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                f"SELECT {LOOP_RUNS_TABLE.select_list} FROM loop_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return _loop_run_from_row(row) if row else None

    def renew_execution_lease(
        self,
        run_id: str,
        *,
        owner: str,
        lease_seconds: float = 180.0,
        now: float | None = None,
    ) -> bool:
        """Extend an unexpired execution lease held by the same driver."""
        current_time = time.time() if now is None else float(now)
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE loop_runs
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND terminal_state = ''
                  AND lease_owner = ? AND lease_expires_at > ?
                """,
                (
                    current_time + max(1.0, float(lease_seconds)),
                    current_time,
                    run_id,
                    owner,
                    current_time,
                ),
            )
        return cursor.rowcount == 1

    def claim_active_for_execution_mode(
        self,
        execution_mode: str,
        *,
        owner: str,
        limit: int = 50,
        lease_seconds: float = 180.0,
        now: float | None = None,
        updated_before: float | None = None,
    ) -> list[LoopRunState]:
        current_time = time.time() if now is None else now
        stale_clause = ""
        params: list[Any] = [execution_mode, owner, current_time]
        if updated_before is not None:
            stale_clause = " AND updated_at <= ?"
            params.append(float(updated_before))
        params.append(limit)
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            ids = [
                str(row[0])
                for row in conn.execute(
                    f"""
                    SELECT id FROM loop_runs
                    WHERE terminal_state = ''
                      AND json_extract(evidence_json, '$.execution_mode') = ?
                      AND (lease_owner = '' OR lease_owner = ? OR lease_expires_at <= ?)
                      {stale_clause}
                    ORDER BY updated_at ASC LIMIT ?
                    """,
                    params,
                ).fetchall()
            ]
            for run_id in ids:
                conn.execute(
                    """
                    UPDATE loop_runs
                    SET lease_owner = ?, lease_expires_at = ?, version = version + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (owner, current_time + max(1.0, lease_seconds), current_time, run_id),
                )
            if not ids:
                return []
            placeholders = ", ".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT {LOOP_RUNS_TABLE.select_list} FROM loop_runs "
                f"WHERE id IN ({placeholders}) ORDER BY updated_at ASC",
                ids,
            ).fetchall()
        return [_loop_run_from_row(row) for row in rows]

    def active_execution_lease_owners(self) -> list[str]:
        """Return durable execution owners without interpreting their identity."""
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT lease_owner FROM loop_runs
                WHERE terminal_state = ''
                  AND lease_owner <> ''
                  AND lease_expires_at > 0
                ORDER BY lease_owner
                """
            ).fetchall()
        return [str(row[0]) for row in rows]

    def release_execution_leases_for_owners(
        self,
        owners: list[str] | tuple[str, ...] | set[str],
        *,
        reason: str,
        now: float | None = None,
    ) -> list[str]:
        """Release leases whose externally observed owner is unavailable."""
        normalized = sorted({str(owner).strip() for owner in owners if str(owner).strip()})
        if not normalized:
            return []
        current_time = time.time() if now is None else float(now)
        placeholders = ", ".join("?" for _ in normalized)
        released: list[str] = []
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT {LOOP_RUNS_TABLE.select_list}
                FROM loop_runs
                WHERE terminal_state = ''
                  AND lease_owner IN ({placeholders})
                ORDER BY updated_at ASC
                """,
                normalized,
            ).fetchall()
            for row in rows:
                current = _loop_run_from_row(row)
                cursor = conn.execute(
                    """
                    UPDATE loop_runs
                    SET lease_owner = '', lease_expires_at = 0,
                        updated_at = ?, version = version + 1
                    WHERE id = ? AND terminal_state = '' AND lease_owner = ?
                    """,
                    (current_time, current.run_id, current.lease_owner),
                )
                if cursor.rowcount != 1:
                    continue
                released.append(current.run_id)
                _insert_event(
                    conn,
                    replace(
                        current,
                        updated_at=current_time,
                        version=current.version + 1,
                        lease_owner="",
                        lease_expires_at=0.0,
                    ),
                    "loop.execution_lease_released",
                    evidence={
                        "reason": reason,
                        "previous_owner": current.lease_owner,
                        "previous_expires_at": current.lease_expires_at,
                    },
                )
        return released

    def release_execution_lease(self, run_id: str, *, owner: str) -> bool:
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE loop_runs
                SET lease_owner = '', lease_expires_at = 0, version = version + 1,
                    updated_at = ?
                WHERE id = ? AND lease_owner = ?
                """,
                (time.time(), run_id, owner),
            )
        return cursor.rowcount == 1

    def release_expired_execution_leases(
        self,
        *,
        now: float | None = None,
        limit: int = 100,
    ) -> list[str]:
        """Clear leases whose owner can no longer safely transition the loop.

        Expiry is the durable ownership boundary: the loop state remains
        resumable, while a later driver may claim it atomically.  Releasing
        the old owner prevents stale foreground work from permanently
        breaching lease-health SLOs.
        """
        current_time = time.time() if now is None else float(now)
        released: list[str] = []
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT {LOOP_RUNS_TABLE.select_list}
                FROM loop_runs
                WHERE terminal_state = ''
                  AND lease_owner <> ''
                  AND lease_expires_at > 0
                  AND lease_expires_at <= ?
                ORDER BY lease_expires_at ASC LIMIT ?
                """,
                (current_time, max(1, int(limit))),
            ).fetchall()
            for row in rows:
                current = _loop_run_from_row(row)
                cursor = conn.execute(
                    """
                    UPDATE loop_runs
                    SET lease_owner = '', lease_expires_at = 0,
                        updated_at = ?, version = version + 1
                    WHERE id = ? AND terminal_state = '' AND lease_owner = ?
                      AND lease_expires_at > 0 AND lease_expires_at <= ?
                    """,
                    (
                        current_time,
                        current.run_id,
                        current.lease_owner,
                        current_time,
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                released.append(current.run_id)
                _insert_event(
                    conn,
                    replace(
                        current,
                        updated_at=current_time,
                        version=current.version + 1,
                        lease_owner="",
                        lease_expires_at=0.0,
                    ),
                    "loop.execution_lease_released",
                    evidence={
                        "reason": "execution_lease_expired",
                        "previous_owner": current.lease_owner,
                        "expired_at": current.lease_expires_at,
                    },
                )
        return released

    def reopen_for_resume(self, run_id: str) -> LoopRunState:
        """Reopen a paused loop at EXECUTE after its external gate is resolved."""
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
                version=current.version + 1,
            )
            cursor = conn.execute(
                """
                UPDATE loop_runs SET node = ?, terminal_state = '', updated_at = ?,
                    version = ? WHERE id = ? AND version = ?
                """,
                (
                    str(reopened.node),
                    reopened.updated_at,
                    reopened.version,
                    run_id,
                    current.version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("loop state changed concurrently")
            _insert_event(
                conn,
                reopened,
                "loop.resumed",
                evidence={"previous_terminal_state": str(current.terminal_state)},
            )
        return reopened

    def reopen_resource_pause(self, run_id: str) -> LoopRunState:
        """Reopen a transient background resource pause at its original node."""
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT {LOOP_RUNS_TABLE.select_list} FROM loop_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"loop run not found: {run_id}")
            current = _loop_run_from_row(row)
            grant = current.evidence.get("resource_grant")
            reason = str(grant.get("reason") or "") if isinstance(grant, dict) else ""
            if str(current.terminal_state) != str(
                LoopTerminalState.PAUSED
            ) or not is_retryable_resource_pause(grant):
                raise ValueError("loop run is not at a transient resource pause")
            raw_node = str(current.evidence.get("resource_resume_node") or "")
            if raw_node not in {str(LoopNode.PLAN), str(LoopNode.EXECUTE), str(LoopNode.EVALUATE)}:
                raise ValueError("transient resource pause has no valid resume node")
            now = time.time()
            reopened = replace(
                current,
                node=raw_node,
                terminal_state="",
                evidence={
                    **current.evidence,
                    "resource_retry": {
                        "reason": reason,
                        "resumed_at": now,
                        "resume_node": raw_node,
                    },
                },
                updated_at=now,
                version=current.version + 1,
            )
            cursor = conn.execute(
                """
                UPDATE loop_runs
                SET node = ?, terminal_state = '', evidence_json = ?, updated_at = ?,
                    version = ? WHERE id = ? AND version = ?
                """,
                (
                    raw_node,
                    _json_dumps(reopened.evidence),
                    now,
                    reopened.version,
                    run_id,
                    current.version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("loop state changed concurrently")
            _insert_event(
                conn,
                reopened,
                "loop.resource_retry",
                evidence=reopened.evidence["resource_retry"],
            )
        return reopened

    def reopen_retryable_pause(self, run_id: str) -> LoopRunState:
        """Reopen one due typed retry gate without changing model semantics."""
        snapshot = self.get_run(run_id)
        if snapshot is None:
            raise KeyError(f"loop run not found: {run_id}")
        if not isinstance(snapshot.evidence.get("retry_gate"), dict):
            # Resource-gateway pauses predate typed retry gates and retain
            # their original validation and resume event contract.
            return self.reopen_resource_pause(run_id)
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT {LOOP_RUNS_TABLE.select_list} FROM loop_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"loop run not found: {run_id}")
            current = _loop_run_from_row(row)
            gate = current.evidence.get("retry_gate")
            if str(current.terminal_state) != str(LoopTerminalState.PAUSED) or not isinstance(
                gate, dict
            ):
                raise ValueError("loop run is not at a typed retry pause")
            if gate.get("decision") != "pause":
                raise ValueError("retry gate is not paused")
            raw_node = str(gate.get("resume_node") or "")
            if raw_node not in {
                str(LoopNode.PLAN),
                str(LoopNode.EXECUTE),
                str(LoopNode.EVALUATE),
            }:
                raise ValueError("retry pause has no valid resume node")
            now = time.time()
            retry_facts = {
                "kind": str(gate.get("kind") or ""),
                "reason": str(gate.get("reason") or ""),
                "retry_count": gate.get("retry_count"),
                "max_retries": gate.get("max_retries"),
                "resumed_at": now,
                "resume_node": raw_node,
            }
            reopened = replace(
                current,
                node=raw_node,
                terminal_state="",
                evidence={
                    **current.evidence,
                    "durable_retry": retry_facts,
                },
                updated_at=now,
                version=current.version + 1,
            )
            cursor = conn.execute(
                """
                UPDATE loop_runs
                SET node = ?, terminal_state = '', evidence_json = ?, updated_at = ?,
                    version = ? WHERE id = ? AND version = ?
                """,
                (
                    raw_node,
                    _json_dumps(reopened.evidence),
                    now,
                    reopened.version,
                    run_id,
                    current.version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("loop state changed concurrently")
            _insert_event(
                conn,
                reopened,
                "loop.durable_retry",
                evidence=retry_facts,
            )
        return reopened

    def reject_external_gate(
        self,
        run_id: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> LoopRunState:
        """Terminate a paused approval gate after an explicit rejection.

        External approval decisions are not ordinary state-graph edges, so
        they need a narrow durable transition that is strict about the source
        state and idempotent for repeated rejection commands.
        """
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT {LOOP_RUNS_TABLE.select_list} FROM loop_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"loop run not found: {run_id}")
            current = _loop_run_from_row(row)
            if str(current.terminal_state) == str(LoopTerminalState.CANCELLED):
                return current
            if str(current.terminal_state) not in {
                str(LoopTerminalState.PAUSED),
                str(LoopTerminalState.WAITING_APPROVAL),
            }:
                raise ValueError(
                    "loop run is not waiting at an external approval gate: "
                    f"{current.terminal_state}"
                )
            now = time.time()
            next_state = replace(
                current,
                terminal_state=LoopTerminalState.CANCELLED,
                evidence={**current.evidence, **(evidence or {})},
                updated_at=now,
                version=current.version + 1,
                lease_owner="",
                lease_expires_at=0.0,
            )
            cursor = conn.execute(
                """
                UPDATE loop_runs
                SET terminal_state = ?, evidence_json = ?, updated_at = ?, version = ?,
                    lease_owner = '', lease_expires_at = 0
                WHERE id = ? AND version = ?
                """,
                (
                    str(LoopTerminalState.CANCELLED),
                    _json_dumps(next_state.evidence),
                    now,
                    next_state.version,
                    run_id,
                    current.version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("loop state changed concurrently")
            _insert_event(
                conn,
                next_state,
                "loop.approval_rejected",
                evidence=evidence,
            )
        return next_state

    def cancel_external_wait(
        self,
        run_id: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> LoopRunState:
        """Cancel a loop that is parked at an external wait boundary."""
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT {LOOP_RUNS_TABLE.select_list} FROM loop_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"loop run not found: {run_id}")
            current = _loop_run_from_row(row)
            if str(current.terminal_state) == str(LoopTerminalState.CANCELLED):
                return current
            if str(current.terminal_state) not in {
                str(LoopTerminalState.PAUSED),
                str(LoopTerminalState.WAITING_APPROVAL),
            }:
                raise ValueError(
                    f"loop run is not waiting at an external boundary: {current.terminal_state}"
                )
            now = time.time()
            next_state = replace(
                current,
                terminal_state=LoopTerminalState.CANCELLED,
                evidence={**current.evidence, **(evidence or {})},
                updated_at=now,
                version=current.version + 1,
                lease_owner="",
                lease_expires_at=0.0,
            )
            cursor = conn.execute(
                """
                UPDATE loop_runs
                SET terminal_state = ?, evidence_json = ?, updated_at = ?, version = ?,
                    lease_owner = '', lease_expires_at = 0
                WHERE id = ? AND version = ?
                """,
                (
                    str(LoopTerminalState.CANCELLED),
                    _json_dumps(next_state.evidence),
                    now,
                    next_state.version,
                    run_id,
                    current.version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("loop state changed concurrently")
            _insert_event(
                conn,
                next_state,
                "loop.cancelled",
                evidence=evidence,
            )
        return next_state

    def complete_external_delivery(
        self,
        run_id: str,
        *,
        success: bool,
        evidence: dict[str, Any] | None = None,
    ) -> LoopRunState:
        """Close only a connector-delivery pause from an authoritative receipt."""
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT {LOOP_RUNS_TABLE.select_list} FROM loop_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"loop run not found: {run_id}")
            current = _loop_run_from_row(row)
            target = LoopTerminalState.CONVERGED if success else LoopTerminalState.FAILED
            if str(current.terminal_state) == str(target):
                return current
            action = str(current.evidence.get("action") or "")
            is_delivery_pause = (
                str(current.terminal_state) == str(LoopTerminalState.PAUSED)
                and action == "connector_outbound"
            )
            if not is_delivery_pause:
                raise ValueError(
                    "external delivery receipt requires a delivery pause: "
                    f"{current.terminal_state}/{action}"
                )
            now = time.time()
            next_state = replace(
                current,
                terminal_state=target,
                evidence={**current.evidence, **(evidence or {})},
                updated_at=now,
                version=current.version + 1,
                lease_owner="",
                lease_expires_at=0.0,
            )
            cursor = conn.execute(
                """
                UPDATE loop_runs
                SET terminal_state = ?, evidence_json = ?, updated_at = ?, version = ?,
                    lease_owner = '', lease_expires_at = 0
                WHERE id = ? AND version = ?
                """,
                (
                    str(target),
                    _json_dumps(next_state.evidence),
                    now,
                    next_state.version,
                    run_id,
                    current.version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("loop state changed concurrently")
            _insert_event(
                conn,
                next_state,
                "loop.delivery_succeeded" if success else "loop.delivery_failed",
                evidence=evidence,
            )
        return next_state

    def fail_active_run(
        self,
        run_id: str,
        *,
        lease_owner: str,
        evidence: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> LoopRunState:
        """Fail a non-terminal loop after an uncaught execution exception."""
        if not lease_owner:
            raise ValueError("loop failure requires a lease owner")
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT {LOOP_RUNS_TABLE.select_list} FROM loop_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"loop run not found: {run_id}")
            current = _loop_run_from_row(row)
            if current.is_terminal():
                return current
            current_time = time.time() if now is None else now
            if current.lease_owner != lease_owner or current.lease_expires_at <= current_time:
                raise RuntimeError("loop execution lease is not owned by this worker")
            next_state = replace(
                current,
                terminal_state=LoopTerminalState.FAILED,
                evidence={**current.evidence, **(evidence or {})},
                updated_at=current_time,
                version=current.version + 1,
                lease_owner="",
                lease_expires_at=0.0,
            )
            cursor = conn.execute(
                """
                UPDATE loop_runs
                SET terminal_state = ?, evidence_json = ?, updated_at = ?, version = ?,
                    lease_owner = '', lease_expires_at = 0
                WHERE id = ? AND version = ? AND lease_owner = ?
                  AND lease_expires_at > ?
                """,
                (
                    str(LoopTerminalState.FAILED),
                    _json_dumps(next_state.evidence),
                    current_time,
                    next_state.version,
                    run_id,
                    current.version,
                    lease_owner,
                    current_time,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("loop state changed concurrently")
            _insert_event(conn, next_state, "loop.execution_failed", evidence=evidence)
        return next_state

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

    def list_waiting_approval(self, *, limit: int = 1000) -> list[LoopRunState]:
        """List durable approval waits for ownership and expiry reconciliation."""
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT {LOOP_RUNS_TABLE.select_list}
                FROM loop_runs
                WHERE terminal_state = ?
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (str(LoopTerminalState.WAITING_APPROVAL), max(1, int(limit))),
            ).fetchall()
        return [_loop_run_from_row(row) for row in rows]

    def list_current_for_goals(
        self,
        goal_ids: list[str] | tuple[str, ...] | set[str],
        *,
        limit: int = 50,
    ) -> list[LoopRunState]:
        """List active or externally paused loops for an explicit goal scope."""
        scoped_ids = tuple(dict.fromkeys(str(item) for item in goal_ids if str(item)))
        if not scoped_ids:
            return []
        rows: list[tuple[Any, ...]] = []
        with connect(self.db_path) as conn:
            for offset in range(0, len(scoped_ids), 500):
                chunk = scoped_ids[offset : offset + 500]
                placeholders = ", ".join("?" for _ in chunk)
                rows.extend(
                    conn.execute(
                        f"""
                        SELECT {LOOP_RUNS_TABLE.select_list}
                        FROM loop_runs
                        WHERE goal_id IN ({placeholders})
                          AND terminal_state IN ('', ?, ?)
                        """,
                        [
                            *chunk,
                            str(LoopTerminalState.PAUSED),
                            str(LoopTerminalState.WAITING_APPROVAL),
                        ],
                    ).fetchall()
                )
        states = sorted(
            (_loop_run_from_row(row) for row in rows),
            key=lambda state: state.updated_at,
            reverse=True,
        )
        return states[: max(1, int(limit))]

    def list_active_for_execution_mode(
        self,
        execution_mode: str,
        *,
        limit: int = 50,
    ) -> list[LoopRunState]:
        """Return active loops explicitly owned by one execution driver."""
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT {LOOP_RUNS_TABLE.select_list}
                FROM loop_runs
                WHERE terminal_state = ''
                  AND json_extract(evidence_json, '$.execution_mode') = ?
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (execution_mode, limit),
            ).fetchall()
        return [_loop_run_from_row(row) for row in rows]

    def list_retryable_background_pauses(
        self,
        *,
        now: float | None = None,
        limit: int = 50,
    ) -> list[LoopRunState]:
        """Return due background pauses caused only by transient resource gates."""
        current_time = time.time() if now is None else now
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT {LOOP_RUNS_TABLE.select_list}
                FROM loop_runs
                WHERE terminal_state = ?
                  AND json_extract(evidence_json, '$.execution_mode') = 'background'
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (str(LoopTerminalState.PAUSED), limit * 5),
            ).fetchall()
        ready: list[LoopRunState] = []
        for row in rows:
            state = _loop_run_from_row(row)
            grant = state.evidence.get("resource_grant")
            if not is_retryable_resource_pause(grant):
                continue
            if not isinstance(grant, dict):
                continue
            try:
                retry_after = max(0.0, float(grant.get("retry_after_seconds") or 0.0))
            except (TypeError, ValueError):
                retry_after = 0.0
            if state.updated_at + retry_after > current_time:
                continue
            ready.append(state)
            if len(ready) >= limit:
                break
        return ready

    def list_retryable_pauses(
        self,
        *,
        now: float | None = None,
        limit: int = 50,
    ) -> list[LoopRunState]:
        """Return due typed retries plus legacy background resource pauses."""
        current_time = time.time() if now is None else now
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT {LOOP_RUNS_TABLE.select_list}
                FROM loop_runs
                WHERE terminal_state = ?
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (str(LoopTerminalState.PAUSED), limit * 5),
            ).fetchall()
        ready: list[LoopRunState] = []
        for row in rows:
            state = _loop_run_from_row(row)
            gate = state.evidence.get("retry_gate")
            retry_after = 0.0
            if isinstance(gate, dict) and gate.get("decision") == "pause":
                retryable = True
                try:
                    retry_after = max(
                        0.0,
                        float(gate.get("retry_after_seconds") or 0.0),
                    )
                except (TypeError, ValueError):
                    retry_after = 0.0
            else:
                retryable = False
                grant = state.evidence.get("resource_grant")
                retryable = (
                    state.evidence.get("execution_mode") == "background"
                    and is_retryable_resource_pause(grant)
                )
                if retryable and isinstance(grant, dict):
                    try:
                        retry_after = max(
                            0.0,
                            float(grant.get("retry_after_seconds") or 0.0),
                        )
                    except (TypeError, ValueError):
                        retry_after = 0.0
            if not retryable or state.updated_at + retry_after > current_time:
                continue
            ready.append(state)
            if len(ready) >= limit:
                break
        return ready

    def list_releasable_resource_scope_ids(
        self,
        *,
        now: float | None = None,
    ) -> list[str]:
        """Return loop-run scopes that are not owned by a live execution lease."""
        current_time = time.time() if now is None else float(now)
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id FROM loop_runs
                WHERE terminal_state <> ''
                   OR lease_owner = ''
                   OR lease_expires_at <= ?
                """,
                (current_time,),
            ).fetchall()
        return [str(row[0]) for row in rows]

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

    def list_by_goal_filtered(
        self,
        goal_id: str,
        *,
        terminal_states: tuple[LoopTerminalState | str, ...] = (),
        evidence_action: str = "",
        limit: int | None = 50,
    ) -> list[LoopRunState]:
        clauses = ["goal_id = ?"]
        params: list[Any] = [goal_id]
        if terminal_states:
            placeholders = ", ".join("?" for _ in terminal_states)
            clauses.append(f"terminal_state IN ({placeholders})")
            params.extend(str(item) for item in terminal_states)
        if evidence_action:
            clauses.append("json_extract(evidence_json, '$.action') = ?")
            params.append(evidence_action)
        limit_clause = ""
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(max(1, int(limit)))
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT {LOOP_RUNS_TABLE.select_list}
                FROM loop_runs
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC{limit_clause}
                """,
                params,
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
        lease_owner: str = "",
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
            if lease_owner and (
                current.lease_owner != lease_owner or current.lease_expires_at <= time.time()
            ):
                raise RuntimeError("loop execution lease is not owned by this driver")
            next_state = replace(
                current.transition(
                    node=node,
                    checkpoint_id=checkpoint_id,
                    terminal_state=terminal_state,
                    evidence=evidence,
                ),
                version=current.version + 1,
                lease_owner="" if str(terminal_state).strip() else current.lease_owner,
                lease_expires_at=0.0 if str(terminal_state).strip() else current.lease_expires_at,
            )
            cursor = conn.execute(
                """
                UPDATE loop_runs
                SET node = ?, terminal_state = ?, checkpoint_id = ?, attempt = ?,
                    parent_run_id = ?, child_run_ids_json = ?, locked_resources_json = ?,
                    evidence_json = ?, updated_at = ?, version = ?, lease_owner = ?,
                    lease_expires_at = ?
                WHERE id = ? AND version = ?
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
                    next_state.version,
                    next_state.lease_owner,
                    next_state.lease_expires_at,
                    run_id,
                    current.version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("loop state changed concurrently")
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

    def latest_checkpoint(
        self,
        run_id: str,
        *,
        node: LoopNode | str = "",
        input_key: str = "",
    ) -> LoopCheckpoint | None:
        clauses = ["run_id = ?"]
        params: list[Any] = [run_id]
        if node:
            clauses.append("node = ?")
            params.append(str(node))
        if input_key:
            clauses.append("json_type(inputs_json, ?) IS NOT NULL")
            params.append(f"$.{input_key}")
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"""
                SELECT {LOOP_CHECKPOINTS_TABLE.select_list}
                FROM loop_checkpoints
                WHERE {" AND ".join(clauses)}
                ORDER BY created_at DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return LoopCheckpoint(*row) if row else None

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
        state.version,
        state.lease_owner,
        state.lease_expires_at,
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
        version=int(row[13]),
        lease_owner=str(row[14]),
        lease_expires_at=float(row[15]),
    )


def _migrate_loop_runs_v3(conn: Any) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(loop_runs)").fetchall()}
    if not columns:
        return
    additions = (
        ("version", "INTEGER NOT NULL DEFAULT 0"),
        ("lease_owner", "TEXT NOT NULL DEFAULT ''"),
        ("lease_expires_at", "REAL NOT NULL DEFAULT 0"),
    )
    for name, declaration in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE loop_runs ADD COLUMN {name} {declaration}")


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
        Column("version", "INTEGER", nullable=False, default="0"),
        Column("lease_owner", "TEXT", nullable=False, default="''"),
        Column("lease_expires_at", "REAL", nullable=False, default="0"),
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


def is_retryable_resource_pause(grant: object) -> bool:
    """The resource protocol, rather than a reason-name allowlist, owns retryability."""
    return isinstance(grant, dict) and str(grant.get("decision") or "") == "pause"
