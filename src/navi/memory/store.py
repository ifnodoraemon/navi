"""MemoryStore: governed memory CRUD + recall + learning pipelines."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from ..paths import db_paths
from ..hooks import HookDecision, HookEvent, HookRegistry
from ..text_utils import truncate_middle
from .models import (
    ACTIVE_MEMORY_CONTEXT_LIMIT,
    ACTIVE_STATUSES,
    LEARNABLE_MEMORY_TYPES,
    MEMORY_STATUSES,
    MEMORY_TYPES,
    MemoryConflict,
    MemoryItem,
    MemoryRecall,
)
from .provider import MemoryProvider, SQLiteMemoryProvider

# TYPE_CHECKING-only imports kept in the methods that need them to avoid
# import cycles at module load time.
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .provider import SessionAlias, StoredMessage

logger = logging.getLogger("navi.memory")

MEMORY_CONFIDENCE_DECAY_TYPES = frozenset({"preference", "fact", "semantic"})
MEMORY_CONFIDENCE_DECAY_GRACE_SECONDS = 90 * 24 * 60 * 60
MEMORY_CONFIDENCE_DECAY_DELTA = 0.05
MEMORY_CONFIDENCE_DECAY_STALE_THRESHOLD = 0.2
MEMORY_GRAPH_SYNC_LIMIT = 1000
MEMORY_GRAPH_EDGE_RELATIONS = (
    "has_memory_type",
    "has_memory_status",
    "has_memory_scope",
    "contradicts",
    "supersedes",
    "superseded_by",
)


class MemoryStore:
    def __init__(
        self,
        home: Path,
        provider: MemoryProvider | None = None,
        embedding_service: object | None = None,
    ):
        self.home = home
        self.memory_dir = home / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.provider = provider or SQLiteMemoryProvider(db_paths(home).memory)
        self._embedding_service = embedding_service


    # ------------------------------------------------------------------ writes

    def add_item(
        self,
        memory_type: str,
        content: str,
        *,
        source: str,
        scope: str = "global",
        status: str = "proposed",
        confidence: float = 0.5,
        last_verified_at: float | None = None,
        expires_at: float = 0.0,
        metadata: dict | None = None,
        reason: str = "",
        provenance: str = "",
    ) -> MemoryItem:
        memory_type = memory_type.strip().lower()
        status = status.strip().lower()
        content = content.strip()
        source = source.strip()
        resolved_scope = scope.strip() or "global"
        reason = reason.strip()
        provenance = provenance.strip()
        if memory_type not in MEMORY_TYPES:
            raise ValueError(f"Unsupported memory type: {memory_type}")
        if status not in MEMORY_STATUSES:
            raise ValueError(f"Unsupported memory status: {status}")
        if not content:
            raise ValueError("memory content is required")
        if not source:
            raise ValueError("memory source is required")
        if not reason:
            raise ValueError("memory reason is required")
        if not provenance:
            raise ValueError("memory provenance is required")
        metadata = dict(metadata or {})
        self._assert_memory_write_allowed(
            memory_type=memory_type,
            status=status,
            scope=resolved_scope,
            source=source,
            confidence=max(0.0, min(1.0, confidence)),
            content_chars=len(content),
            metadata_keys=sorted(metadata.keys()),
        )
        now = time.time()

        item = MemoryItem(
            id=uuid.uuid4().hex,
            type=memory_type,
            status=status,
            scope=resolved_scope,
            content=content,
            source=source,
            confidence=max(0.0, min(1.0, confidence)),
            created_at=now,
            updated_at=now,
            last_verified_at=last_verified_at or 0.0,
            expires_at=expires_at,
            metadata=metadata or {},
            reason=reason,
            provenance=provenance,
        )
        # Atomic: recompute contradictions and store in one transaction so a
        # concurrent writer cannot interleave between read and write.
        return self.provider.store_item_with_contradictions(item)

    def list_items(
        self,
        *,
        memory_type: str | None = None,
        status: str | None = None,
        allowed_scopes: set[str] | frozenset[str] | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        items = self.provider.get_items(
            memory_type=memory_type,
            status=status,
            allowed_scopes=allowed_scopes,
            limit=limit,
        )
        return items

    def list_conflicts(
        self,
        *,
        limit: int = 50,
        allowed_scopes: set[str] | frozenset[str] | None = None,
    ) -> list[MemoryConflict]:
        items = self.list_items(allowed_scopes=allowed_scopes, limit=1000)
        by_id = {item.id: item for item in items}
        conflicts: list[MemoryConflict] = []
        for item in items:
            for relation in ("contradicts", "supersedes"):
                for conflicting_item_id in _metadata_id_list(item.metadata.get(relation)):
                    conflicting_item = by_id.get(conflicting_item_id) or self.get_item(
                        conflicting_item_id
                    )
                    conflicts.append(
                        MemoryConflict(
                            item=item,
                            relation=relation,
                            conflicting_item_id=conflicting_item_id,
                            conflicting_item=conflicting_item,
                            status=_memory_conflict_status(item, conflicting_item),
                            reason=f"metadata.{relation}",
                        )
                    )
                    if len(conflicts) >= limit:
                        return conflicts
        return conflicts

    def conflicts_for_item(self, item_id: str, *, limit: int = 50) -> list[MemoryConflict]:
        return [
            conflict
            for conflict in self.list_conflicts(limit=1000)
            if conflict.item.id == item_id or conflict.conflicting_item_id == item_id
        ][:limit]

    def get_item(self, item_id: str) -> MemoryItem | None:
        return self.provider.get_item(item_id)

    # Lifecycle transitions a governed memory item may take. Validating these
    # keeps the lifecycle invariant (principle 10): e.g. a revoked item cannot
    # silently return to active without resolving the contradiction.
    _ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
        "proposed": frozenset({"accepted", "active", "revoked", "archived"}),
        "accepted": frozenset({"active", "archived", "revoked"}),
        "active": frozenset({"contradicted", "stale", "archived", "revoked"}),
        "contradicted": frozenset({"active", "archived", "revoked"}),
        "stale": frozenset({"active", "archived", "revoked"}),
        "archived": frozenset({"active", "revoked"}),
        "revoked": frozenset({"proposed", "archived"}),
    }

    def set_status(self, item_id: str, status: str) -> MemoryItem | None:
        status = status.strip().lower()
        if status not in MEMORY_STATUSES:
            raise ValueError(f"Unsupported memory status: {status}")
        current = self.get_item(item_id)
        if current is not None and current.status != status:
            allowed = self._ALLOWED_TRANSITIONS.get(current.status, frozenset())
            if status not in allowed:
                raise ValueError(
                    f"invalid memory lifecycle transition: "
                    f"{current.status} -> {status}"
                )
            # A lifecycle transition is a governed memory write: route it
            # through the ``before_memory_write`` hook so policy can observe
            # or block the new status (principle 9/10).
            self._assert_memory_write_allowed(
                memory_type=current.type,
                status=status,
                scope=current.scope,
                source=current.source,
                confidence=max(0.0, min(1.0, current.confidence)),
                content_chars=len(current.content),
                metadata_keys=sorted(current.metadata.keys()),
            )
        self.provider.update_item(item_id, status=status, updated_at=time.time())
        return self.get_item(item_id)

    def verify_item(self, item_id: str) -> MemoryItem | None:
        # Verification is a governed memory write (principle 9/10): route it
        # through the ``before_memory_write`` hook so policy can observe or
        # block verification events, consistent with ``reduce_confidence``.
        current = self.get_item(item_id)
        if current is not None:
            self._assert_memory_write_allowed(
                memory_type=current.type,
                status=current.status,
                scope=current.scope,
                source=current.source,
                confidence=max(0.0, min(1.0, current.confidence)),
                content_chars=len(current.content),
                metadata_keys=sorted(current.metadata.keys()),
            )
        self.provider.update_item(item_id, last_verified_at=time.time(), updated_at=time.time())
        return self.get_item(item_id)

    def delete_item(self, item_id: str) -> None:
        """Hard-delete a memory item.

        Principle 9/10: deletion is a governed memory write. Prefer
        :meth:`set_status` with ``archived`` for governance rollback; hard
        deletion is reserved for storage hygiene and gated through the
        ``before_memory_write`` hook so policy can block it."""
        current = self.get_item(item_id)
        if current is not None:
            self._assert_memory_write_allowed(
                memory_type=current.type,
                status="archived",
                scope=current.scope,
                source=current.source,
                confidence=max(0.0, min(1.0, current.confidence)),
                content_chars=len(current.content),
                metadata_keys=sorted(current.metadata.keys()),
            )
        self.provider.delete_item(item_id)

    def expire_items(self, *, now: float | None = None, limit: int = 1000) -> dict[str, Any]:
        """Move expired active working-memory items out of the active set.

        Expiration is explicit GC, not prompt compression: expired active items
        become ``stale`` and expired proposed/accepted/contradicted items are
        archived. The item remains auditable, but no longer renders into active
        working memory or recall.
        """
        current_time = time.time() if now is None else now
        expired: list[dict[str, str]] = []
        for item in self.list_items(limit=limit):
            if not item.expires_at or item.expires_at > current_time:
                continue
            if item.status in {"archived", "revoked", "stale"}:
                continue
            next_status = "stale" if item.status == "active" else "archived"
            updated = self.set_status(item.id, next_status)
            if updated is not None:
                expired.append(
                    {
                        "id": updated.id,
                        "previous_status": item.status,
                        "status": updated.status,
                    }
                )
        return {
            "expired_count": len(expired),
            "expired_items": expired,
        }

    def supersede_item(
        self,
        item_id: str,
        *,
        replacement_item_id: str,
        reason: str,
    ) -> MemoryItem | None:
        """Archive an item and record the item that superseded it."""
        current = self.get_item(item_id)
        replacement = self.get_item(replacement_item_id)
        if current is None or replacement is None:
            return None
        if not reason.strip():
            raise ValueError("supersede reason is required")
        metadata = dict(current.metadata)
        superseded_by = _metadata_id_list(metadata.get("superseded_by"))
        if replacement_item_id not in superseded_by:
            superseded_by.append(replacement_item_id)
        metadata["superseded_by"] = superseded_by
        metadata["supersede_reason"] = reason.strip()
        metadata["superseded_at"] = time.time()
        self._assert_memory_write_allowed(
            memory_type=current.type,
            status="archived",
            scope=current.scope,
            source=current.source,
            confidence=max(0.0, min(1.0, current.confidence)),
            content_chars=len(current.content),
            metadata_keys=sorted(metadata.keys()),
        )
        updated = replace(
            current,
            status="archived",
            metadata=metadata,
            updated_at=time.time(),
        )
        self.provider.store_item(updated)
        return self.get_item(item_id)

    def garbage_collect(self, *, now: float | None = None, limit: int = 1000) -> dict[str, Any]:
        """Run bounded working-memory GC and return objective facts."""
        current_time = time.time() if now is None else now
        expired = self.expire_items(now=now, limit=limit)
        decayed = self.decay_inactive_confidence(now=current_time, limit=limit)
        active_count = len(
            [
                item
                for item in self.list_items(limit=limit)
                if item.status in ACTIVE_STATUSES
                and (not item.expires_at or item.expires_at > current_time)
            ]
        )
        return {
            "gc": "working_memory",
            "expired_count": expired["expired_count"],
            "expired_items": expired["expired_items"],
            "decayed_count": decayed["decayed_count"],
            "decayed_items": decayed["decayed_items"],
            "active_count": active_count,
        }

    def decay_inactive_confidence(
        self,
        *,
        now: float | None = None,
        limit: int = 1000,
        grace_seconds: float = MEMORY_CONFIDENCE_DECAY_GRACE_SECONDS,
        delta: float = MEMORY_CONFIDENCE_DECAY_DELTA,
        stale_threshold: float = MEMORY_CONFIDENCE_DECAY_STALE_THRESHOLD,
    ) -> dict[str, Any]:
        """Lower confidence for old learnable facts without touching constraints.

        This is explicit maintenance, not prompt-time filtering. Constraints and
        negative memories are excluded so durable must/must-not boundaries do not
        silently weaken with age.
        """
        current_time = time.time() if now is None else now
        decayed: list[dict[str, Any]] = []
        for item in self.list_items(limit=limit):
            if item.type not in MEMORY_CONFIDENCE_DECAY_TYPES:
                continue
            if item.status != "active":
                continue
            if item.expires_at and item.expires_at <= current_time:
                continue
            anchor = (
                _metadata_float(item.metadata, "last_recalled_at")
                or item.last_verified_at
                or item.updated_at
                or item.created_at
            )
            age_seconds = max(0.0, current_time - anchor)
            if age_seconds < grace_seconds:
                continue
            previous_confidence = max(0.0, min(1.0, item.confidence))
            new_confidence = max(0.0, previous_confidence - max(0.0, delta))
            self._assert_memory_write_allowed(
                memory_type=item.type,
                status=item.status,
                scope=item.scope,
                source=item.source,
                confidence=new_confidence,
                content_chars=len(item.content),
                metadata_keys=sorted(item.metadata.keys()),
            )
            self.provider.update_item(
                item.id,
                confidence=new_confidence,
                updated_at=current_time,
            )
            final = self.get_item(item.id)
            final_status = final.status if final is not None else item.status
            if new_confidence <= stale_threshold:
                stale = self.set_status(item.id, "stale")
                final_status = stale.status if stale is not None else final_status
            decayed.append(
                {
                    "id": item.id,
                    "type": item.type,
                    "previous_confidence": previous_confidence,
                    "confidence": new_confidence,
                    "previous_status": item.status,
                    "status": final_status,
                    "age_seconds": age_seconds,
                }
            )
        return {"decayed_count": len(decayed), "decayed_items": decayed}

    def record_activation(
        self,
        item_id: str,
        *,
        now: float | None = None,
        reason: str,
        provenance: str,
    ) -> MemoryItem | None:
        """Record that a memory item was explicitly used by a planner/tool path."""
        current = self.get_item(item_id)
        if current is None:
            return None
        reason = reason.strip()
        provenance = provenance.strip()
        if not reason:
            raise ValueError("activation reason is required")
        if not provenance:
            raise ValueError("activation provenance is required")
        current_time = time.time() if now is None else now
        metadata = dict(current.metadata)
        previous_count = _metadata_int(metadata, "recall_count")
        metadata["last_recalled_at"] = current_time
        metadata["recall_count"] = previous_count + 1
        metadata["activation_reason"] = reason
        metadata["activation_provenance"] = provenance
        self._assert_memory_write_allowed(
            memory_type=current.type,
            status=current.status,
            scope=current.scope,
            source=current.source,
            confidence=max(0.0, min(1.0, current.confidence)),
            content_chars=len(current.content),
            metadata_keys=sorted(metadata.keys()),
        )
        self.provider.store_item(
            replace(
                current,
                metadata=metadata,
                updated_at=current_time,
            )
        )
        return self.get_item(item_id)

    def sync_semantic_graph(
        self,
        *,
        graph_store: Any | None = None,
        limit: int = MEMORY_GRAPH_SYNC_LIMIT,
    ) -> dict[str, Any]:
        """Sync typed memory records into the graph index.

        memory.db remains the source of truth. The graph is a derived semantic
        index that can be rebuilt by this method, so memory writes do not gain a
        cross-database atomicity dependency.
        """
        if graph_store is None:
            from ..graph import GraphStore

            graph_store = GraphStore(self.home)

        items = self.list_items(limit=limit)
        synced: list[dict[str, Any]] = []
        relation_counts = {relation: 0 for relation in MEMORY_GRAPH_EDGE_RELATIONS}
        for item in items:
            item_node = graph_store.upsert(
                "MemoryItem",
                item.id,
                _memory_graph_item_data(item),
            )
            type_node = graph_store.upsert(
                "MemoryType",
                item.type,
                {"memory_type": item.type},
            )
            status_node = graph_store.upsert(
                "MemoryStatus",
                item.status,
                {"status": item.status},
            )
            scope_node = graph_store.upsert(
                "MemoryScope",
                item.scope,
                {"scope": item.scope},
            )
            edges: list[tuple[str, str, dict[str, Any]]] = [
                (type_node.id, "has_memory_type", {"memory_id": item.id, "type": item.type}),
                (
                    status_node.id,
                    "has_memory_status",
                    {"memory_id": item.id, "status": item.status},
                ),
                (
                    scope_node.id,
                    "has_memory_scope",
                    {"memory_id": item.id, "scope": item.scope},
                ),
            ]
            for target_id in _metadata_id_list(item.metadata.get("contradicts")):
                target_node = _upsert_memory_graph_placeholder(graph_store, target_id)
                edges.append(
                    (
                        target_node.id,
                        "contradicts",
                        {"memory_id": item.id, "target_memory_id": target_id},
                    )
                )
            for target_id in _metadata_id_list(item.metadata.get("supersedes")):
                target_node = _upsert_memory_graph_placeholder(graph_store, target_id)
                edges.append(
                    (
                        target_node.id,
                        "supersedes",
                        {"memory_id": item.id, "target_memory_id": target_id},
                    )
                )
            for target_id in _metadata_id_list(item.metadata.get("superseded_by")):
                target_node = _upsert_memory_graph_placeholder(graph_store, target_id)
                edges.append(
                    (
                        target_node.id,
                        "superseded_by",
                        {"memory_id": item.id, "target_memory_id": target_id},
                    )
                )
            graph_edges = graph_store.replace_edges_for_source(
                item_node.id,
                MEMORY_GRAPH_EDGE_RELATIONS,
                edges,
            )
            for edge in graph_edges:
                relation_counts[edge.relation] = relation_counts.get(edge.relation, 0) + 1
            synced.append(
                {
                    "memory_id": item.id,
                    "graph_node_id": item_node.id,
                    "edge_count": len(graph_edges),
                }
            )
        return {
            "semantic_graph": "memory",
            "synced_count": len(synced),
            "limit": limit,
            "edge_relations": list(MEMORY_GRAPH_EDGE_RELATIONS),
            "relation_counts": relation_counts,
            "items": synced,
        }

    def restore_item(self, item_dict: dict) -> None:
        if isinstance(item_dict.get("metadata"), str):
            item_dict["metadata"] = json.loads(item_dict["metadata"])
        item = MemoryItem(**item_dict)
        # Restoring a memory item (e.g. evolution rollback) is still a memory
        # write and must pass the before_memory_write hook so policy can block
        # it (principle 9/16). The original id/metadata are preserved because
        # this restores a previously-governed item rather than creating a new one.
        self._assert_memory_write_allowed(
            memory_type=item.type,
            status=item.status,
            scope=item.scope,
            source=item.source,
            confidence=max(0.0, min(1.0, item.confidence)),
            content_chars=len(item.content),
            metadata_keys=sorted(item.metadata.keys()),
        )
        # FP-3/L5: recompute ``contradicts`` against the current active items
        # atomically (read + contradict + store in one transaction) so a
        # restored item cannot silently introduce unresolved contradictions
        # after other items have changed.
        normalized = MemoryItem(
            id=item.id,
            type=item.type,
            status=item.status,
            scope=item.scope,
            content=item.content,
            source=item.source,
            confidence=max(0.0, min(1.0, item.confidence)),
            created_at=item.created_at,
            updated_at=item.updated_at,
            last_verified_at=item.last_verified_at,
            expires_at=item.expires_at,
            metadata=dict(item.metadata),
            reason=item.reason,
            provenance=item.provenance,
        )
        self.provider.store_item_with_contradictions(normalized)

    def reduce_confidence(self, item_id: str, *, delta: float = 0.1) -> None:
        """Reduce the confidence of a memory item by ``delta``.

        Used by :class:`EvolutionEngine.rollback` to lower the trust level of
        a memory item whose originating evolution was rejected.  The write is
        routed through the ``before_memory_write`` hook so policy can observe
        or block it (principle 9/10)."""
        current = self.get_item(item_id)
        if current is None:
            return
        new_confidence = max(0.0, current.confidence - delta)
        self._assert_memory_write_allowed(
            memory_type=current.type,
            status=current.status,
            scope=current.scope,
            source=current.source,
            confidence=new_confidence,
            content_chars=len(current.content),
            metadata_keys=sorted(current.metadata.keys()),
        )
        self.provider.update_item(
            item_id,
            confidence=new_confidence,
            updated_at=time.time(),
        )

    def _assert_memory_write_allowed(
        self,
        *,
        memory_type: str,
        status: str,
        scope: str,
        source: str,
        confidence: float,
        content_chars: int,
        metadata_keys: list[str],
    ) -> None:
        blocked = _blocking_hook(
            HookRegistry(self.home).run(
                HookEvent(
                    event="before_memory_write",
                    payload={
                        "type": memory_type,
                        "status": status,
                        "scope": scope,
                        "source": source,
                        "confidence": confidence,
                        "content_chars": content_chars,
                        "metadata_keys": metadata_keys,
                    },
                )
            )
        )
        if blocked is not None:
            raise ValueError(blocked.reason_code or f"hook_blocked:{blocked.hook}")

    def _parse_json_learnings(self, response_raw: str) -> list[dict]:
        try:
            data = json.loads(response_raw)
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(data, dict):
            return []

        learnings = data.get("learnings")
        if not isinstance(learnings, list):
            return []
        return learnings

    # ------------------------------------------------------------------ recall

    def recall(
        self,
        query: str,
        *,
        limit: int = 8,
        goal: str = "",
        allowed_scopes: set[str] | frozenset[str] | None = None,
    ) -> list[MemoryRecall]:
        now = time.time()
        fts_query = f"{query} {goal}".strip()
        if not fts_query:
            return []

        fts_results = self.provider.search_fts(
            fts_query,
            limit=limit * 3,
            allowed_scopes=allowed_scopes,
        )
        if not fts_results:
            return []

        ranked_candidates: list[tuple[str, float, list[str]]] = []
        seen_candidate_ids: set[str] = set()
        for item_id, rank in fts_results:
            if item_id in seen_candidate_ids:
                continue
            ranked_candidates.append((item_id, abs(rank), [f"fts_rank={rank:.4f}"]))
            seen_candidate_ids.add(item_id)

        graph_neighbors = self._semantic_graph_neighbors(
            tuple(item_id for item_id, _rank in fts_results),
            limit=limit * 3,
        )
        for item_id, reasons in graph_neighbors.items():
            if item_id in seen_candidate_ids:
                continue
            ranked_candidates.append((item_id, 0.0, reasons))
            seen_candidate_ids.add(item_id)

        selected = []
        for item_id, score, reasons in ranked_candidates:
            item = self.get_item(item_id)
            if not item:
                continue
            if allowed_scopes is not None and item.scope not in allowed_scopes:
                continue
            if item.status not in ACTIVE_STATUSES or (item.expires_at and item.expires_at <= now):
                continue
            selected.append(
                MemoryRecall(
                    item=item,
                    score=score,
                    reasons=reasons,
                )
            )
            if len(selected) >= limit:
                break

        if not selected:
            return []
        conflicts = self.list_conflicts(
            limit=1000,
            allowed_scopes=allowed_scopes,
        )
        return [self._with_conflict_reasons(recall, conflicts) for recall in selected]

    def _semantic_graph_neighbors(
        self,
        seed_item_ids: tuple[str, ...],
        *,
        limit: int,
    ) -> dict[str, list[str]]:
        graph_db = db_paths(self.home).graph
        if not seed_item_ids or not graph_db.exists():
            return {}
        neighbors: dict[str, list[str]] = {}
        try:
            graph_uri = f"{graph_db.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(graph_uri, uri=True, timeout=30.0)) as conn:
                conn.execute("PRAGMA query_only=ON")
                has_edges = conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'graph_edges'
                    """
                ).fetchone()
                if not has_edges:
                    return {}
                for seed_id in seed_item_ids:
                    seed_node = conn.execute(
                        """
                        SELECT id FROM graph_nodes
                        WHERE type = 'MemoryItem' AND name = ?
                        """,
                        (seed_id,),
                    ).fetchone()
                    if seed_node is None:
                        continue
                    graph_node_id = str(seed_node[0])
                    rows = conn.execute(
                        """
                        SELECT relation, source_id, target_id
                        FROM graph_edges
                        WHERE source_id = ? OR target_id = ?
                        ORDER BY updated_at DESC
                        LIMIT ?
                        """,
                        (graph_node_id, graph_node_id, limit),
                    ).fetchall()
                    for relation, source_id, target_id in rows:
                        other_graph_id = target_id if source_id == graph_node_id else source_id
                        other = conn.execute(
                            """
                            SELECT name FROM graph_nodes
                            WHERE id = ? AND type = 'MemoryItem'
                            """,
                            (other_graph_id,),
                        ).fetchone()
                        if other is None:
                            continue
                        other_memory_id = str(other[0])
                        if other_memory_id == seed_id:
                            continue
                        direction = "out" if source_id == graph_node_id else "in"
                        reason = (
                            f"semantic_graph_neighbor={direction}:"
                            f"{relation}:{seed_id}"
                        )
                        reasons = neighbors.setdefault(other_memory_id, [])
                        if reason not in reasons:
                            reasons.append(reason)
        except Exception:
            logger.debug("semantic graph neighbor recall failed", exc_info=True)
            return {}
        return neighbors

    def render_context(
        self,
        query: str,
        *,
        limit: int = ACTIVE_MEMORY_CONTEXT_LIMIT,
        goal: str = "",
        allowed_scopes: set[str] | frozenset[str] | None = None,
    ) -> str:
        recalls = self.recall(
            query,
            limit=limit,
            goal=goal,
            allowed_scopes=allowed_scopes,
        )
        if not recalls:
            return ""
        lines: list[str] = []
        for recall in recalls:
            item = recall.item
            verified = (
                time.strftime("%Y-%m-%d", time.localtime(item.last_verified_at))
                if item.last_verified_at
                else "unverified"
            )
            reasons = ", ".join(recall.reasons)
            lines.append(
                f"- [type={item.type} scope={item.scope} confidence={item.confidence:.2f} "
                f"score={recall.score:.4f} verified={verified} id={item.id}] "
                f"{truncate_middle(item.content, ACTIVE_MEMORY_CONTEXT_LIMIT)}"
            )
            if reasons:
                lines.append(f"  reasons: {reasons}")
        return "\n".join(lines)

    def active_constraints(
        self,
        *,
        limit: int = 100,
        allowed_scopes: set[str] | frozenset[str] | None = None,
    ) -> list[MemoryItem]:
        """Return all active constraint-type memories, unconditionally.

        Principle 12: durable must/must-not rules must survive context compression
        and be reloaded from the store before the agent acts. Unlike recall(),
        this is NOT query-scored -- constraints are always in scope regardless of
        semantic similarity to the current message, so a long or summarized
        conversation cannot drop them.
        """
        now = time.time()
        return [
            item
            for item in self.list_items(
                memory_type="constraint",
                allowed_scopes=allowed_scopes,
                limit=limit,
            )
            if item.status in ACTIVE_STATUSES and (not item.expires_at or item.expires_at > now)
        ]

    def render_durable_constraints(
        self,
        *,
        limit: int = 100,
        allowed_scopes: set[str] | frozenset[str] | None = None,
    ) -> str:
        """Render active constraints as authoritative facts for the planner.

        Returns "" when there are no active constraints. The output is trusted
        runtime state sourced from Navi's own governed memory store, not from
        untrusted conversation text."""
        constraints = self.active_constraints(
            limit=limit,
            allowed_scopes=allowed_scopes,
        )
        if not constraints:
            return ""
        lines = ["Durable constraints (reloaded from governed memory; always in effect):"]
        for item in constraints:
            verified = (
                time.strftime("%Y-%m-%d", time.localtime(item.last_verified_at))
                if item.last_verified_at
                else "unverified"
            )
            lines.append(
                f"- [scope={item.scope} confidence={item.confidence:.2f} "
                f"source={item.source} verified={verified} id={item.id}] {item.content}"
            )
        return "\n".join(lines)

    def render_working_memory(self, *, goal_store: Any = None, limit: int = 20) -> str:
        """Render a pinned working-memory snapshot for the planner.

        Gap D: extend the per-step durable-constraints injection to also
        carry working state — the active goal + objective, the phase, and
        key run facts. This is the "pin working memory so it survives
        context compression" piece: every step the planner sees a fresh
        snapshot of what it is currently working on, regardless of how
        much conversation history has been truncated or summarized.

        Returns "" when there is no goal store or no active goals.
        """
        if goal_store is None:
            return ""
        from ..lifecycle import Phase as _GoalPhase  # local import to avoid cycle

        try:
            goals = goal_store.list(limit=limit)
        except Exception:
            return ""
        active = [g for g in goals if g.phase == str(_GoalPhase.RUNNING)]
        if not active:
            return ""
        lines = ["Working memory snapshot (reloaded every step; survives context compression):"]
        for g in active:
            lines.append(
                f"- [goal_id={g.id} phase={g.phase} run_id={g.run_id}] "
                f"objective={g.objective}"
            )
        return "\n".join(lines)

    # --------------------------------------------------------------- sessions

    def new_session_id(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]

    def create_session(self, *, alias: str | None = None) -> str:
        session_id = self.new_session_id()
        if alias:
            self.set_session_alias(alias, session_id)
        return session_id

    def set_session_alias(self, alias: str, session_id: str) -> SessionAlias:
        now = time.time()
        existing = self.get_session_alias(alias)
        created_at = existing.created_at if existing else now
        self.provider.set_session_alias(alias, session_id, created_at, now)
        return self.get_session_alias(alias) or SessionAlias(alias, session_id, created_at, now)

    def get_session_alias(self, alias: str) -> SessionAlias | None:
        return self.provider.get_session_alias(alias)

    def current_session_id(self, alias: str) -> str:
        current = self.get_session_alias(alias)
        if current:
            return current.session_id
        return self.create_session(alias=alias)

    def rotate_session(self, alias: str) -> SessionAlias:
        return self.set_session_alias(alias, self.new_session_id())

    def list_session_aliases(self, *, limit: int = 50) -> list[SessionAlias]:
        return self.provider.list_session_aliases(limit)

    def add_message(self, session_id: str, role: str, content: str) -> None:
        self.provider.add_message(session_id, role, content, time.time())

    def list_sessions(self) -> list[str]:
        return self.provider.list_sessions()

    def get_messages(self, session_id: str, limit: int = 50) -> list[StoredMessage]:
        return self.provider.get_messages(session_id, limit)

    def clear_messages(self, session_id: str) -> int:
        """Delete all messages for *session_id*.

        conversation context is polluted with failing assumptions.
        context when the loop triggers ``REFLECT_AND_REPLAN``. The next
        planner call rebuilds context from durable constraints + working
        memory snapshot only. Returns the number of deleted rows.
        """
        return self.provider.clear_messages(session_id)

    def _list_active_learnable_items(self) -> list[MemoryItem]:
        active_items: list[MemoryItem] = []
        for item_type in LEARNABLE_MEMORY_TYPES:
            active_items.extend(
                self.list_items(
                    memory_type=item_type,
                    status="active",
                    limit=ACTIVE_MEMORY_CONTEXT_LIMIT,
                )
            )
        return active_items

    # --------------------------------------------------------- apply learnings

    def _apply_learnings(
        self,
        learnings: list,
        active_items: list,
        *,
        source: str,
        provenance: str,
        ledger_run_id: str,
        add_reason_fallback: str,
    ) -> list:
        """Apply extracted add/revoke learnings with full provenance + ledger.

        Shared by conversation consolidation and run reflection so the two
        learning pipelines cannot drift (DRY, principle 1.1).
        """
        from ..evolution import EvolutionLedger

        ledger = EvolutionLedger(self.home)
        affected_items: list = []
        seen_memory_keys = {
            (item.type, item.content.strip().lower()) for item in active_items
        }
        for learning in learnings:
            if not isinstance(learning, dict):
                continue
            action = str(learning.get("action", "")).strip().lower()
            if action == "add":
                item = self._apply_add_learning(
                    learning,
                    seen_memory_keys,
                    source=source,
                    provenance=provenance,
                    add_reason_fallback=add_reason_fallback,
                )
                if item is None:
                    continue
                affected_items.append(item)
                ledger.record(
                    run_id=ledger_run_id,
                    target_type="memory_item",
                    target_id=item.id,
                    reason="memory_learning_added",
                    before="",
                    after=json.dumps(item.__dict__, default=str),
                )
            elif action == "revoke":
                item_id = str(learning.get("id", "")).strip()
                if not item_id:
                    continue
                old_item = self.get_item(item_id)
                if old_item and old_item.status in ["active", "accepted"]:
                    updated_item = self.set_status(item_id, "revoked")
                    if updated_item:
                        affected_items.append(updated_item)
                    ledger.record(
                        run_id=ledger_run_id,
                        target_type="memory_item",
                        target_id=item_id,
                        reason=str(learning.get("reason") or "memory_learning_revoked"),
                        before=json.dumps(old_item.__dict__, default=str),
                        after="revoked",
                    )
        return affected_items

    def _apply_add_learning(
        self,
        learning: dict,
        seen_memory_keys: set,
        *,
        source: str,
        provenance: str,
        add_reason_fallback: str,
    ):
        m_type = str(learning.get("type", "")).strip().lower()
        content = str(learning.get("content", "")).strip()
        if not content or m_type not in LEARNABLE_MEMORY_TYPES:
            return None
        memory_key = (m_type, content.lower())
        if memory_key in seen_memory_keys:
            return None
        try:
            conf_val = float(learning.get("confidence", 0.7))
        except (ValueError, TypeError):
            conf_val = 0.7
        contradicts = learning.get("contradicts", [])
        if not isinstance(contradicts, list):
            contradicts = []
        # LLM-extracted learnings are proposals, not durable accepted memory.
        # Promotion to accepted/active must go through the governed memory or
        # evolution path with review evidence.
        promoted_status = "proposed"
        new_item = self.add_item(
            memory_type=m_type,
            content=content,
            source=source,
            scope="global",
            status=promoted_status,
            confidence=conf_val,
            metadata={"contradicts": contradicts} if contradicts else {},
            reason=str(learning.get("reason") or add_reason_fallback),
            provenance=provenance,
        )
        seen_memory_keys.add(memory_key)
        return new_item

    # ------------------------------------------------------------- scoring

    @staticmethod
    def _item_from_row(row: tuple) -> MemoryItem:
        values = list(row)
        values[11] = json.loads(values[11] or "{}")
        return MemoryItem(*values)



    @staticmethod
    def _with_conflict_reasons(
        recall: MemoryRecall, conflicts: list[MemoryConflict]
    ) -> MemoryRecall:
        related = tuple(
            conflict
            for conflict in conflicts
            if conflict.item.id == recall.item.id or conflict.conflicting_item_id == recall.item.id
        )
        if not related:
            return recall
        reasons = list(recall.reasons)
        unresolved = [conflict for conflict in related if conflict.status == "unresolved"]
        if unresolved:
            reasons.append(f"unresolved_memory_conflicts={len(unresolved)}")
        else:
            reasons.append(f"declared_memory_conflicts={len(related)}")
        return MemoryRecall(
            item=recall.item, score=recall.score, reasons=reasons, conflicts=related
        )


def _blocking_hook(decisions: list[HookDecision]) -> HookDecision | None:
    return next((decision for decision in decisions if decision.decision == "block"), None)


def _metadata_id_list(value) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _metadata_float(metadata: dict, key: str) -> float:
    try:
        return max(0.0, float(metadata.get(key) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _metadata_int(metadata: dict, key: str) -> int:
    try:
        return max(0, int(metadata.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _memory_graph_item_data(item: MemoryItem) -> dict[str, Any]:
    return {
        "memory_id": item.id,
        "memory_type": item.type,
        "status": item.status,
        "scope": item.scope,
        "source": item.source,
        "confidence": item.confidence,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "last_verified_at": item.last_verified_at,
        "expires_at": item.expires_at,
        "metadata": dict(item.metadata),
        "reason": item.reason,
        "provenance": item.provenance,
        "content": item.content,
        "content_preview": truncate_middle(item.content, 240),
        "placeholder": False,
    }


def _upsert_memory_graph_placeholder(graph_store: Any, memory_id: str):
    return graph_store.upsert(
        "MemoryItem",
        memory_id,
        {
            "memory_id": memory_id,
            "placeholder": True,
        },
    )


def _memory_conflict_status(item: MemoryItem, conflicting_item: MemoryItem | None) -> str:
    if conflicting_item is None:
        return "missing_target"
    if item.status in ACTIVE_STATUSES and conflicting_item.status in ACTIVE_STATUSES:
        return "unresolved"
    if item.status in ACTIVE_STATUSES:
        return "resolved"
    return "inactive"


def _render_conflict_summary(conflicts: tuple[MemoryConflict, ...]) -> str:
    if not conflicts:
        return ""
    return ",".join(
        f"{conflict.relation}:{conflict.conflicting_item_id}:{conflict.status}"
        for conflict in conflicts[:3]
    )
