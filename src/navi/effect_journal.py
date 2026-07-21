from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect
from .paths import db_paths


@dataclass(frozen=True)
class EffectReservation:
    status: str
    result: dict[str, Any] | None = None

    @property
    def acquired(self) -> bool:
        return self.status == "acquired"


class EffectJournal:
    """Crash-safe idempotency records for mutating capability invocations."""

    def __init__(self, home: Path):
        self.db_path = db_paths(home).loop_runs
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS loop_effects (
                    effect_key TEXT PRIMARY KEY,
                    loop_run_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    result_json TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_loop_effects_run "
                "ON loop_effects(loop_run_id, updated_at)"
            )

    def reserve(
        self,
        *,
        effect_key: str,
        loop_run_id: str,
        tool: str,
        owner: str,
        lease_seconds: float = 900.0,
        now: float | None = None,
    ) -> EffectReservation:
        current_time = time.time() if now is None else now
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT status, owner, lease_expires_at, result_json
                FROM loop_effects WHERE effect_key = ?
                """,
                (effect_key,),
            ).fetchone()
            if row is not None:
                status, _current_owner, expires_at, result_json = (
                    str(row[0]),
                    str(row[1]),
                    float(row[2]),
                    str(row[3]),
                )
                if status == "completed":
                    return EffectReservation("replay", _json_dict(result_json))
                if status == "uncertain":
                    return EffectReservation("uncertain")
                if status == "active" and expires_at > current_time:
                    return EffectReservation("busy")
                if status == "active":
                    conn.execute(
                        """
                        UPDATE loop_effects
                        SET status = 'uncertain', lease_expires_at = 0,
                            error = 'lease_expired_without_reconciliation', updated_at = ?
                        WHERE effect_key = ? AND status = 'active'
                        """,
                        (current_time, effect_key),
                    )
                    return EffectReservation("uncertain")
                conn.execute(
                    """
                    UPDATE loop_effects
                    SET status = 'active', owner = ?, lease_expires_at = ?,
                        result_json = '', error = '', updated_at = ?
                    WHERE effect_key = ?
                    """,
                    (owner, current_time + lease_seconds, current_time, effect_key),
                )
                return EffectReservation("acquired")
            conn.execute(
                """
                INSERT INTO loop_effects(
                    effect_key, loop_run_id, tool, status, owner, lease_expires_at,
                    result_json, error, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?, '', '', ?, ?)
                """,
                (
                    effect_key,
                    loop_run_id,
                    tool,
                    owner,
                    current_time + lease_seconds,
                    current_time,
                    current_time,
                ),
            )
        return EffectReservation("acquired")

    def complete(self, effect_key: str, *, owner: str, result: dict[str, Any]) -> None:
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE loop_effects
                SET status = 'completed', result_json = ?, error = '',
                    lease_expires_at = 0, updated_at = ?
                WHERE effect_key = ? AND owner = ? AND status = 'active'
                """,
                (json.dumps(result, ensure_ascii=False, sort_keys=True), time.time(), effect_key, owner),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("effect journal ownership changed before completion")

    def fail(self, effect_key: str, *, owner: str, error: str) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE loop_effects
                SET status = 'uncertain', error = ?, lease_expires_at = 0, updated_at = ?
                WHERE effect_key = ? AND owner = ? AND status = 'active'
                """,
                (error, time.time(), effect_key, owner),
            )

    def abandon(self, effect_key: str, *, owner: str) -> None:
        """Release a reservation when execution never started."""
        with connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM loop_effects WHERE effect_key = ? AND owner = ? AND status = 'active'",
                (effect_key, owner),
            )


def _json_dict(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
