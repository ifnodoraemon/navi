from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .db import connect
from typing import Any


@dataclass(frozen=True)
class GraphNode:
    id: str
    type: str
    name: str
    data: dict[str, Any]
    created_at: float
    updated_at: float


class GraphStore:
    def __init__(self, home: Path):
        home.mkdir(parents=True, exist_ok=True)
        self.db_path = home / "graph.db"
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_nodes (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(type, name)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_type ON graph_nodes(type, updated_at)")

    def upsert(self, node_type: str, name: str, data: dict[str, Any]) -> GraphNode:
        now = time.time()
        existing = self.get_by_name(node_type, name)
        merged = {**(existing.data if existing else {}), **data}
        if existing:
            with connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE graph_nodes SET data = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(merged, sort_keys=True), now, existing.id),
                )
            return self.get(existing.id) or existing
        node = GraphNode(
            id=uuid.uuid4().hex,
            type=node_type,
            name=name,
            data=merged,
            created_at=now,
            updated_at=now,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO graph_nodes(id, type, name, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    node.id,
                    node.type,
                    node.name,
                    json.dumps(node.data, sort_keys=True),
                    node.created_at,
                    node.updated_at,
                ),
            )
        return node

    def get(self, node_id: str) -> GraphNode | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT id, type, name, data, created_at, updated_at FROM graph_nodes WHERE id = ?",
                (node_id,),
            ).fetchone()
        return self._node_from_row(row) if row else None

    def get_by_name(self, node_type: str, name: str) -> GraphNode | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, type, name, data, created_at, updated_at
                FROM graph_nodes WHERE type = ? AND name = ?
                """,
                (node_type, name),
            ).fetchone()
        return self._node_from_row(row) if row else None

    def list(self, node_type: str | None = None, *, limit: int = 100) -> list[GraphNode]:
        with connect(self.db_path) as conn:
            if node_type:
                rows = conn.execute(
                    """
                    SELECT id, type, name, data, created_at, updated_at
                    FROM graph_nodes WHERE type = ? ORDER BY updated_at DESC LIMIT ?
                    """,
                    (node_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, type, name, data, created_at, updated_at
                    FROM graph_nodes ORDER BY updated_at DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [self._node_from_row(row) for row in rows]

    def replace_data(self, node_id: str, data: dict[str, Any]) -> GraphNode | None:
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE graph_nodes SET data = ?, updated_at = ? WHERE id = ?",
                (json.dumps(data, sort_keys=True), time.time(), node_id),
            )
        return self.get(node_id)

    def delete(self, node_id: str) -> None:
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM graph_nodes WHERE id = ?", (node_id,))

    @staticmethod
    def _node_from_row(row: tuple) -> GraphNode:
        return GraphNode(
            id=row[0],
            type=row[1],
            name=row[2],
            data=json.loads(row[3] or "{}"),
            created_at=row[4],
            updated_at=row[5],
        )
