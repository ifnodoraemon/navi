"""Watch persistence mixin for RunStore."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from ..db import connect
from ..schema import Column, Table
from .models import Watch, _require_workspace

if TYPE_CHECKING:
    pass

WATCHES_TABLE = Table(
    "watches",
    [
        Column("id", "TEXT", primary_key=True),
        Column("cron", "TEXT", nullable=False),
        Column("prompt", "TEXT", nullable=False),
        Column("peer_id", "TEXT", nullable=False),
        Column("sender_id", "TEXT", nullable=False),
        Column("enabled", "INTEGER", nullable=False),
        Column("next_run_at", "REAL", nullable=False),
        Column("last_run_at", "REAL", nullable=False),
        Column("created_at", "REAL", nullable=False),
        Column("updated_at", "REAL", nullable=False),
        Column("workspace", "TEXT", nullable=False),
        Column("kind", "TEXT", nullable=False, default="'recurring'"),
    ],
)


class WatchStoreMixin:
    """Mixin providing watch persistence methods to RunStore.

    Requires:
    - db_path: Path (instance attribute, provided by RunStore.__init__)
    """

    db_path: Path

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

    def list_watches(self, *, limit: int = 50, offset: int = 0) -> list[Watch]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, cron, prompt, peer_id, sender_id, enabled,
                       next_run_at, last_run_at, created_at, updated_at, workspace, kind
                FROM watches ORDER BY updated_at DESC LIMIT ? OFFSET ?
                """,
                (limit, offset),
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

    @staticmethod
    def _watch_from_row(row: tuple) -> Watch:
        values = list(row)
        values[5] = bool(values[5])
        return Watch(*values)
