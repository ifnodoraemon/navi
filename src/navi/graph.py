from __future__ import annotations

import builtins
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .db import connect
from .paths import db_paths
from typing import Any


@dataclass(frozen=True)
class GraphNode:
    id: str
    type: str
    name: str
    data: dict[str, Any]
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class GraphEdge:
    id: str
    source_id: str
    target_id: str
    relation: str
    data: dict[str, Any]
    created_at: float
    updated_at: float


class GraphStore:
    def __init__(self, home: Path):
        home.mkdir(parents=True, exist_ok=True)
        self.db_path = db_paths(home).graph
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_graph_type ON graph_nodes(type, updated_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_edges (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(source_id, target_id, relation)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_graph_edges_source "
                "ON graph_edges(source_id, relation, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_graph_edges_target "
                "ON graph_edges(target_id, relation, updated_at)"
            )

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
            conn.execute(
                "DELETE FROM graph_edges WHERE source_id = ? OR target_id = ?",
                (node_id, node_id),
            )
            conn.execute("DELETE FROM graph_nodes WHERE id = ?", (node_id,))

    def upsert_edge(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        data: dict[str, Any] | None = None,
    ) -> GraphEdge:
        source_id = source_id.strip()
        target_id = target_id.strip()
        relation = relation.strip()
        if not source_id or not target_id or not relation:
            raise ValueError("graph edge source_id, target_id, and relation are required")
        now = time.time()
        edge_data = dict(data or {})
        existing = self.get_edge(source_id, target_id, relation)
        merged = {**(existing.data if existing else {}), **edge_data}
        if existing:
            with connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE graph_edges SET data = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(merged, sort_keys=True), now, existing.id),
                )
            return self.get_edge(source_id, target_id, relation) or existing
        edge = GraphEdge(
            id=uuid.uuid4().hex,
            source_id=source_id,
            target_id=target_id,
            relation=relation,
            data=merged,
            created_at=now,
            updated_at=now,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO graph_edges(
                    id, source_id, target_id, relation, data, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge.id,
                    edge.source_id,
                    edge.target_id,
                    edge.relation,
                    json.dumps(edge.data, sort_keys=True),
                    edge.created_at,
                    edge.updated_at,
                ),
            )
        return edge

    def get_edge(self, source_id: str, target_id: str, relation: str) -> GraphEdge | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, source_id, target_id, relation, data, created_at, updated_at
                FROM graph_edges
                WHERE source_id = ? AND target_id = ? AND relation = ?
                """,
                (source_id, target_id, relation),
            ).fetchone()
        return self._edge_from_row(row) if row else None

    def list_edges(
        self,
        *,
        source_id: str | None = None,
        target_id: str | None = None,
        relation: str | None = None,
        limit: int = 100,
    ) -> builtins.list[GraphEdge]:
        clauses = []
        values: builtins.list[object] = []
        if source_id:
            clauses.append("source_id = ?")
            values.append(source_id)
        if target_id:
            clauses.append("target_id = ?")
            values.append(target_id)
        if relation:
            clauses.append("relation = ?")
            values.append(relation)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, source_id, target_id, relation, data, created_at, updated_at
                FROM graph_edges
                {where}
                ORDER BY updated_at DESC LIMIT ?
                """,
                values,
            ).fetchall()
        return [self._edge_from_row(row) for row in rows]

    def replace_edges_for_source(
        self,
        source_id: str,
        relations: tuple[str, ...],
        edges: builtins.list[tuple[str, str, dict[str, Any]]],
    ) -> builtins.list[GraphEdge]:
        if not relations:
            return [
                self.upsert_edge(source_id, target_id, relation, data)
                for target_id, relation, data in edges
            ]
        placeholders = ", ".join("?" for _ in relations)
        with connect(self.db_path) as conn:
            conn.execute(
                f"DELETE FROM graph_edges WHERE source_id = ? AND relation IN ({placeholders})",
                [source_id, *relations],
            )
        return [
            self.upsert_edge(source_id, target_id, relation, data)
            for target_id, relation, data in edges
        ]

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

    @staticmethod
    def _edge_from_row(row: tuple) -> GraphEdge:
        return GraphEdge(
            id=row[0],
            source_id=row[1],
            target_id=row[2],
            relation=row[3],
            data=json.loads(row[4] or "{}"),
            created_at=row[5],
            updated_at=row[6],
        )
