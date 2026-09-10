"""MemoryStore: governed memory CRUD + recall + learning pipelines."""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path

from ..db import connect
from ..json_utils import json_object
from ..paths import db_paths
from ..hooks import HookDecision, HookEvent, HookRegistry
from ..text_utils import truncate_middle


def _resolve_now(now: float | None) -> float:
    if now is not None:
        return now
    return time.time()


def _effective_param(provided: float | None, fallback: float) -> float:
    if provided is not None:
        return provided
    return fallback


def _format_verification_date(ts: float | None) -> str:
    if not ts:
        return "unverified"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _as_list(val: Any) -> list[Any]:
    if isinstance(val, list):
        return val
    return []


def _where_clause(predicates: list[str]) -> str:
    if not predicates:
        return ""
    return f"WHERE {' AND '.join(predicates)}"
from .models import (
    ACTIVE_MEMORY_CONTEXT_LIMIT,
    ACTIVE_STATUSES,
    LEARNABLE_MEMORY_TYPES,
    MEMORY_STATUSES,
    MEMORY_TYPES,
    MemoryConflict,
    MemoryConsolidationJob,
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
RECALLABLE_STATUSES = frozenset(ACTIVE_STATUSES | {"proposed"})
MEMORY_GRAPH_SYNC_LIMIT = 1000
MEMORY_GRAPH_EDGE_RELATIONS = (
    "has_memory_type",
    "has_memory_status",
    "has_memory_scope",
    "contradicts",
    "supersedes",
    "superseded_by",
)
MEMORY_CONSOLIDATION_HISTORY_RETENTION_SECONDS = 30 * 24 * 60 * 60
_CONSOLIDATION_JOB_COLUMNS = (
    "id, session_id, run_id, source, peer_id, sender_id, status, "
    "owner, lease_expires_at, attempts, error, created_at, updated_at"
)


def _record_job_event(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    event: str,
    from_status: str,
    to_status: str,
    reason: str,
    error: str = "",
    created_at: float,
) -> None:
    conn.execute(
        """
        INSERT INTO memory_consolidation_job_events(
            job_id, event, from_status, to_status, reason, error, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (job_id, event, from_status, to_status, reason, error, created_at),
    )


def _raise(exc: Exception) -> Any:
    raise exc


from ..dynamic_parameters import SYSTEM_DYNAMIC_PARAMETERS

DEFAULT_MEMORY_PARAMETERS: dict[str, float] = SYSTEM_DYNAMIC_PARAMETERS


class MemoryStore:
    def __init__(
        self,
        home: Path,
        provider: MemoryProvider | None = None,
    ):
        self.home = home
        self.memory_dir = home / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.provider = provider or SQLiteMemoryProvider(db_paths(home).memory)
        self._parameters_cache: dict[str, float] = {}
        self._parameters_initialized = False
        self._recent_recall_queries: dict[str, str] = {}

    def _ensure_parameters(self) -> None:
        now = time.time()
        for name, default_val in DEFAULT_MEMORY_PARAMETERS.items():
            self.provider.set_parameter_if_absent(
                name,
                default_val,
                updated_at=now,
                metadata={"reason": "default_initialization"},
            )
        persisted = self.provider.list_parameters()
        self._parameters_cache = {name: val for name, (val, _, _) in persisted.items()}

    def get_parameter(self, name: str, default: float | None = None) -> float:
        self._ensure_parameters()
        fallback = _effective_param(default, DEFAULT_MEMORY_PARAMETERS.get(name, 0.0))
        return float(self._parameters_cache.get(name, fallback))

    def get_parameter_entry(self, name: str) -> dict[str, Any] | None:
        self._ensure_parameters()
        entry = self.provider.get_parameter(name)
        if entry is not None:
            return {"name": name, "value": entry[0], "updated_at": entry[1], "metadata": entry[2]}
        default_val = DEFAULT_MEMORY_PARAMETERS.get(name)
        if default_val is not None:
            return {
                "name": name,
                "value": default_val,
                "updated_at": 0.0,
                "metadata": {"reason": "default"},
            }
        return None

    def set_parameter(
        self,
        name: str,
        value: float,
        *,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._ensure_parameters()
        now = time.time()
        meta = dict(metadata or {})
        meta["reason"] = reason or meta.get("reason", "dynamic_update")
        val = float(value)
        self._parameters_cache[name] = val
        self.provider.set_parameter(name, val, updated_at=now, metadata=meta)

    def list_parameters(self) -> dict[str, dict[str, Any]]:
        self._ensure_parameters()
        entries = self.provider.list_parameters()
        return {
            name: {"name": name, "value": val, "updated_at": updated_at, "metadata": meta}
            for name, (val, updated_at, meta) in entries.items()
        }

    def _adapt_cue_weights_hebbian(self, *, query: str, content: str) -> None:
        """Apply Hebbian plasticity: adapt cue weights toward features that triggered recall.

        Branchless continuous adaptation: if query/content match has zero overlap,
        the effective learning rate smoothly vanishes to zero via continuous soft gating.
        """
        normalized_query = query.strip().lower()
        normalized_content = content.strip().lower()

        query_features = _text_features(normalized_query)
        content_features = _text_features(normalized_content)

        intersection = query_features & content_features
        union = query_features | content_features

        lexical_jaccard = len(intersection) / max(1, len(union))
        query_coverage = len(intersection) / max(1, len(query_features))
        sequence_score = SequenceMatcher(None, normalized_query, normalized_content).ratio()

        valid_gate = float(bool(intersection))
        feature_sum = (query_coverage + lexical_jaccard + sequence_score) * valid_gate
        eps = 1e-4

        current_cov = self.get_parameter("cue_weight_coverage", 0.60)
        current_jac = self.get_parameter("cue_weight_jaccard", 0.25)
        current_seq = self.get_parameter("cue_weight_sequence", 0.15)

        target_cov = (query_coverage * valid_gate + eps * current_cov) / (feature_sum + eps)
        target_jac = (lexical_jaccard * valid_gate + eps * current_jac) / (feature_sum + eps)
        target_seq = (sequence_score * valid_gate + eps * current_seq) / (feature_sum + eps)

        eta = self.get_parameter("hebbian_learning_rate", 0.05) * (feature_sum / (feature_sum + eps))

        new_cov = max(0.01, current_cov + eta * (target_cov - current_cov))
        new_jac = max(0.01, current_jac + eta * (target_jac - current_jac))
        new_seq = max(0.01, current_seq + eta * (target_seq - current_seq))
        norm = max(1e-6, new_cov + new_jac + new_seq)

        norm_cov = round(new_cov / norm, 4)
        norm_jac = round(new_jac / norm, 4)
        norm_seq = round(1.0 - norm_cov - norm_jac, 4)

        self.set_parameter("cue_weight_coverage", norm_cov, reason="hebbian_activation_reinforcement")
        self.set_parameter("cue_weight_jaccard", norm_jac, reason="hebbian_activation_reinforcement")
        self.set_parameter("cue_weight_sequence", norm_seq, reason="hebbian_activation_reinforcement")

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
        (memory_type not in MEMORY_TYPES) and _raise(ValueError(f"Unsupported memory type: {memory_type}"))
        (status not in MEMORY_STATUSES) and _raise(ValueError(f"Unsupported memory status: {status}"))
        (not content) and _raise(ValueError("memory content is required"))
        (not source) and _raise(ValueError("memory source is required"))
        (not reason) and _raise(ValueError("memory reason is required"))
        (not provenance) and _raise(ValueError("memory provenance is required"))
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
        # Contradiction links are model-declared metadata; the store persists
        # them without deriving semantic judgments of its own.
        self.provider.store_item(item)
        return item

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
                    take = len(conflicts) < limit
                    take and conflicts.append(
                        MemoryConflict(
                            item=item,
                            relation=relation,
                            conflicting_item_id=conflicting_item_id,
                            conflicting_item=conflicting_item,
                            status=_memory_conflict_status(item, conflicting_item),
                            reason=f"metadata.{relation}",
                        )
                    )
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
    # keeps the lifecycle invariant: e.g. a revoked item cannot
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
        (status not in MEMORY_STATUSES) and _raise(ValueError(f"Unsupported memory status: {status}"))
        current = self.get_item(item_id)
        is_transition = current is not None and current.status != status
        allowed = self._ALLOWED_TRANSITIONS.get(getattr(current, "status", ""), frozenset())
        (is_transition and status not in allowed) and _raise(
            ValueError(
                f"invalid memory lifecycle transition: "
                f"{getattr(current, 'status', '')} -> {status}"
            )
        )
        is_transition and self._assert_memory_write_allowed(
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
        current = self.get_item(item_id)
        current is not None and self._assert_memory_write_allowed(
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
        current = self.get_item(item_id)
        current is not None and self._assert_memory_write_allowed(
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
        current_time = _resolve_now(now)
        expired: list[dict[str, str]] = []
        for item in self.list_items(limit=limit):
            is_expired = bool(item.expires_at and item.expires_at <= current_time)
            is_active_lifecycle = item.status not in {"archived", "revoked", "stale"}
            should_expire = is_expired and is_active_lifecycle
            next_status = {"active": "stale"}.get(item.status, "archived")
            updated = should_expire and self.set_status(item.id, next_status)
            bool(updated) and expired.append(
                {
                    "id": getattr(updated, "id", item.id),
                    "previous_status": item.status,
                    "status": getattr(updated, "status", next_status),
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
        if not current or not replacement:
            return None
        return self._do_supersede(current, replacement, item_id, replacement_item_id, reason)

    def _do_supersede(
        self,
        current: MemoryItem,
        replacement: MemoryItem,
        item_id: str,
        replacement_item_id: str,
        reason: str,
    ) -> MemoryItem | None:
        clean_reason = reason.strip()
        (not clean_reason) and _raise(ValueError("supersede reason is required"))
        metadata = dict(current.metadata)
        superseded_by = _metadata_id_list(metadata.get("superseded_by"))
        (replacement_item_id not in superseded_by) and superseded_by.append(replacement_item_id)
        metadata["superseded_by"] = superseded_by
        metadata["supersede_reason"] = clean_reason
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
        current_time = _resolve_now(now)
        expired = self.expire_items(now=now, limit=limit)
        decayed = self.decay_inactive_confidence(now=current_time, limit=limit)
        pruned = self.prune_consolidation_history(now=current_time)
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
            "consolidation_jobs_pruned": pruned["pruned_job_count"],
            "consolidation_events_pruned": pruned["pruned_event_count"],
        }

    def prune_consolidation_history(
        self,
        *,
        now: float | None = None,
        retention_seconds: float | None = None,
    ) -> dict[str, int]:
        current_time = _resolve_now(now)
        effective_retention = _effective_param(
            retention_seconds,
            self.get_parameter(
                "consolidation_history_retention_seconds",
                MEMORY_CONSOLIDATION_HISTORY_RETENTION_SECONDS,
            ),
        )
        cutoff = current_time - effective_retention
        with connect(db_paths(self.home).memory) as conn:
            pruned_jobs = conn.execute(
                """
                DELETE FROM memory_consolidation_jobs
                WHERE status = 'completed' AND updated_at < ?
                """,
                (cutoff,),
            ).rowcount
            pruned_events = conn.execute(
                "DELETE FROM memory_consolidation_job_events WHERE created_at < ?",
                (cutoff,),
            ).rowcount
        return {"pruned_job_count": pruned_jobs, "pruned_event_count": pruned_events}

    def decay_inactive_confidence(
        self,
        *,
        now: float | None = None,
        limit: int = 1000,
        grace_seconds: float | None = None,
        delta: float | None = None,
        stale_threshold: float | None = None,
    ) -> dict[str, Any]:
        """Lower confidence for old learnable facts without touching constraints.

        This is explicit maintenance, not prompt-time filtering. Constraints and
        negative memories are excluded so durable must/must-not boundaries do not
        silently weaken with age.
        """
        current_time = _resolve_now(now)
        effective_grace = _effective_param(
            grace_seconds,
            self.get_parameter("decay_grace_seconds", MEMORY_CONFIDENCE_DECAY_GRACE_SECONDS),
        )
        effective_delta_param = _effective_param(
            delta,
            self.get_parameter("decay_base_delta", MEMORY_CONFIDENCE_DECAY_DELTA),
        )
        effective_stale_threshold = _effective_param(
            stale_threshold,
            self.get_parameter("decay_stale_threshold", MEMORY_CONFIDENCE_DECAY_STALE_THRESHOLD),
        )
        stability_scale = self.get_parameter("decay_stability_scale", 1.0)

        decayed: list[dict[str, Any]] = []
        for item in self.list_items(limit=limit):
            is_decayable_type = item.type in MEMORY_CONFIDENCE_DECAY_TYPES
            is_active_status = item.status == "active"
            not_expired = not bool(item.expires_at) or (item.expires_at > current_time)
            eligible_type_and_status = is_decayable_type and is_active_status and not_expired

            anchor = max(
                _metadata_float(item.metadata, "last_recalled_at"),
                item.last_verified_at,
                item.updated_at,
                item.created_at,
            )
            age_seconds = max(0.0, current_time - anchor)
            past_grace = age_seconds >= effective_grace
            should_decay = eligible_type_and_status and past_grace

            previous_confidence = max(0.0, min(1.0, item.confidence))
            activations = float(item.metadata.get("activation_count") or item.metadata.get("recall_count") or 0.0)
            stability = 1.0 + stability_scale * math.log(1.0 + max(0.0, activations))
            effective_delta = max(0.0, effective_delta_param) / stability
            new_confidence = max(0.0, previous_confidence - effective_delta)

            should_decay and self._assert_memory_write_allowed(
                memory_type=item.type,
                status=item.status,
                scope=item.scope,
                source=item.source,
                confidence=new_confidence,
                content_chars=len(item.content),
                metadata_keys=sorted(item.metadata.keys()),
            )
            should_decay and self.provider.update_item(
                item.id,
                confidence=new_confidence,
                updated_at=current_time,
            )
            final = should_decay and self.get_item(item.id)
            final_status = getattr(final, "status", item.status)
            is_stale = should_decay and (new_confidence <= effective_stale_threshold)
            stale = is_stale and self.set_status(item.id, "stale")
            final_status = getattr(stale, "status", final_status)
            should_decay and decayed.append(
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
        query: str | None = None,
    ) -> MemoryItem | None:
        """Record that a memory item was explicitly used by a planner/tool path."""
        current = self.get_item(item_id)
        if not current:
            return None
        return self._do_record_activation(
            current,
            item_id=item_id,
            now=now,
            reason=reason,
            provenance=provenance,
            query=query,
        )

    def _do_record_activation(
        self,
        current: MemoryItem,
        *,
        item_id: str,
        now: float | None,
        reason: str,
        provenance: str,
        query: str | None,
    ) -> MemoryItem | None:
        clean_reason = reason.strip()
        clean_provenance = provenance.strip()
        (not clean_reason) and _raise(ValueError("activation reason is required"))
        (not clean_provenance) and _raise(ValueError("activation provenance is required"))
        current_time = _resolve_now(now)
        metadata = dict(current.metadata)
        previous_count = _metadata_int(metadata, "recall_count")
        metadata["last_recalled_at"] = current_time
        metadata["recall_count"] = previous_count + 1
        metadata["activation_reason"] = clean_reason
        metadata["activation_provenance"] = clean_provenance

        # Dynamic Hebbian synaptic adaptation during runtime usage (continuous gate)
        effective_query = (query or "").strip() or self._recent_recall_queries.pop(item_id, "")
        self._adapt_cue_weights_hebbian(query=effective_query, content=current.content)

        # Long-Term Potentiation (LTP): provisional (proposed) memories explicitly
        # activated by a planner or tool are confirmed and promoted to active status.
        # Boost delta is dynamically tuned rather than hardcoded.
        is_proposed = float(current.status == "proposed")
        new_status = {"proposed": "active"}.get(current.status, current.status)
        ltp_boost = self.get_parameter("ltp_boost_delta", 0.05)
        new_confidence = min(1.0, current.confidence + ltp_boost * is_proposed)

        # Dynamic LTP reinforcement: reward successful promotions during model usage
        new_ltp_boost = round(min(0.20, max(0.01, ltp_boost + 0.002 * is_proposed)), 4)
        bool(is_proposed) and self.set_parameter("ltp_boost_delta", new_ltp_boost, reason="ltp_dynamic_reinforcement")

        self._assert_memory_write_allowed(
            memory_type=current.type,
            status=new_status,
            scope=current.scope,
            source=current.source,
            confidence=max(0.0, min(1.0, new_confidence)),
            content_chars=len(current.content),
            metadata_keys=sorted(metadata.keys()),
        )
        self.provider.store_item(
            replace(
                current,
                status=new_status,
                confidence=new_confidence,
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
        def _get_store():
            from ..graph import GraphStore
            return GraphStore(self.home)

        graph_store = graph_store or _get_store()

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
        item_dict["metadata"] = json_object(item_dict.get("metadata"))
        item = MemoryItem(**item_dict)
        # Restoring a memory item (e.g. evolution rollback) is still a memory
        # write and must pass the before_memory_write hook so policy can block
        # it. The original id/metadata are preserved because
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
        # A rollback restores the previously-governed snapshot, including its
        # model-declared contradiction links, unchanged.
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
        self.provider.store_item(normalized)

    def reduce_confidence(self, item_id: str, *, delta: float | None = None) -> None:
        """Reduce the confidence of a memory item by ``delta``.

        The write is routed through the ``before_memory_write`` hook so policy
        can observe or block it. Evolution rollback does not
        call this method because rollback must restore the exact prior record."""
        current = self.get_item(item_id)
        if not current:
            return None
        self._do_reduce_confidence(current, item_id=item_id, delta=delta)

    def _do_reduce_confidence(
        self, current: MemoryItem, *, item_id: str, delta: float | None
    ) -> None:
        effective_delta = _effective_param(
            delta, self.get_parameter("confidence_reduction_delta", 0.10)
        )
        new_confidence = max(0.0, current.confidence - effective_delta)

        # Dynamic calibration of reduction delta on error signals during usage
        lr = self.get_parameter("hebbian_learning_rate", 0.05)
        calibrated_delta = round(min(0.30, max(0.02, effective_delta + lr * 0.01 * (current.confidence - 0.5))), 4)
        (delta is None) and self.set_parameter(
            "confidence_reduction_delta",
            calibrated_delta,
            reason="confidence_reduction_dynamic_calibration",
        )

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
        (blocked is not None) and _raise(
            ValueError(blocked.reason_code or f"hook_blocked:{blocked.hook}")
        )

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
        return self._do_recall(
            fts_query,
            query=query,
            limit=limit,
            goal=goal,
            allowed_scopes=allowed_scopes,
            now=now,
        )

    def _do_recall(
        self,
        fts_query: str,
        *,
        query: str,
        limit: int,
        goal: str,
        allowed_scopes: set[str] | frozenset[str] | None,
        now: float,
    ) -> list[MemoryRecall]:
        fts_multiplier = self.get_parameter("recall_fts_pool_multiplier", 3.0)
        fts_limit = max(1, int(limit * fts_multiplier))
        fts_results = self.provider.search_fts(
            fts_query,
            limit=fts_limit,
            allowed_scopes=allowed_scopes,
        )

        ranked_candidates: list[tuple[str, float, list[str]]] = []
        seen_candidate_ids: set[str] = set()
        for item_id, rank in fts_results:
            is_new = item_id not in seen_candidate_ids
            is_new and ranked_candidates.append((item_id, abs(rank), [f"fts_rank={rank:.4f}"]))
            is_new and seen_candidate_ids.add(item_id)

        lexical_multiplier = self.get_parameter("recall_lexical_pool_multiplier", 20.0)
        pool_min = self.get_parameter("recall_candidate_pool_min", 200.0)
        candidate_items = self.provider.get_items(
            allowed_scopes=allowed_scopes,
            limit=max(int(pool_min), int(limit * lexical_multiplier)),
        )
        lexical_candidates: list[tuple[str, float, list[str]]] = []
        for item in candidate_items:
            unseen = (item.id not in seen_candidate_ids) and (item.status in RECALLABLE_STATUSES)
            similarity = self._lexical_similarity(fts_query, item.content) * float(unseen)
            has_sim = similarity > 0.0
            has_sim and lexical_candidates.append(
                (
                    item.id,
                    1.0 - similarity,
                    [f"lexical_similarity={similarity:.4f}"],
                )
            )
            has_sim and seen_candidate_ids.add(item.id)
        lexical_candidates.sort(key=lambda item: item[1])
        ranked_candidates.extend(lexical_candidates)

        # Associative graph neighbors and spreading activation from seeds (FTS or top lexical)
        seeds_for_graph: list[str] = [item_id for item_id, _rank in fts_results] or [
            item_id for item_id, _score, _reasons in lexical_candidates[:3]
        ]
        fanout = self.get_parameter("graph_fanout_damping", 3.0)
        graph_neighbors = {}
        if seeds_for_graph:
            graph_neighbors = self._semantic_graph_neighbors(
                tuple(seeds_for_graph),
                limit=max(1, int(limit * fanout)),
            )
        for item_id, reasons in graph_neighbors.items():
            in_seen = item_id in seen_candidate_ids
            actions = {
                True: lambda: self._merge_graph_reasons(ranked_candidates, item_id, reasons),
                False: lambda: (ranked_candidates.append((item_id, 0.0, reasons)), seen_candidate_ids.add(item_id)),
            }
            actions[in_seen]()

        selected: list[MemoryRecall] = []
        for item_id, score, reasons in ranked_candidates:
            recalled_item = self.get_item(item_id)
            eligible = bool(
                recalled_item
                and (allowed_scopes is None or recalled_item.scope in allowed_scopes)
                and recalled_item.status in RECALLABLE_STATUSES
                and (not recalled_item.expires_at or recalled_item.expires_at > now)
            )
            take = eligible and (len(selected) < limit)
            take and selected.append(
                MemoryRecall(
                    item=recalled_item,
                    score=score,
                    reasons=[*reasons, *({"proposed": ["status=proposed"]}.get(recalled_item.status, []))],
                )
            )
            take and self._recent_recall_queries.__setitem__(recalled_item.id, query.strip())
            (len(self._recent_recall_queries) > 500) and self._recent_recall_queries.pop(next(iter(self._recent_recall_queries)))

        conflicts = ()
        if selected:
            conflicts = self.list_conflicts(limit=1000, allowed_scopes=allowed_scopes)
        return [self._with_conflict_reasons(recall, conflicts) for recall in selected]

    @staticmethod
    def _merge_graph_reasons(
        ranked_candidates: list[tuple[str, float, list[str]]],
        item_id: str,
        reasons: list[str],
    ) -> None:
        for existing_id, _score, existing_reasons in ranked_candidates:
            matches = existing_id == item_id
            matches and existing_reasons.extend([r for r in reasons if r not in existing_reasons])

    def _lexical_similarity(
        self,
        query: str,
        content: str,
    ) -> float:
        normalized_query = query.strip().lower()
        normalized_content = content.strip().lower()

        query_features = _text_features(normalized_query)
        content_features = _text_features(normalized_content)

        intersection = query_features & content_features
        union = query_features | content_features

        lexical_jaccard = len(intersection) / max(1, len(union))
        query_coverage = len(intersection) / max(1, len(query_features))
        sequence_score = SequenceMatcher(None, normalized_query, normalized_content).ratio()

        # Dynamic parameter resolution: weights and reinforcement rates are self-tuning
        w_cov = self.get_parameter("cue_weight_coverage", 0.60)
        w_jac = self.get_parameter("cue_weight_jaccard", 0.25)
        w_seq = self.get_parameter("cue_weight_sequence", 0.15)
        tf_rate = self.get_parameter("tf_reinforcement_rate", 0.15)
        tf_max = self.get_parameter("tf_max_extra_boost", 3.0)

        # Continuous simplex normalization preserving dynamic relative ratios
        total_w = max(1e-6, w_cov + w_jac + w_seq)
        nw_cov = w_cov / total_w
        nw_jac = w_jac / total_w
        nw_seq = w_seq / total_w

        extra_occurrences = max(0, sum(normalized_content.count(t) for t in intersection) - len(intersection))
        tf_reinforcement = 1.0 + tf_rate * min(tf_max, float(extra_occurrences))
        activation = (nw_cov * query_coverage * tf_reinforcement) + nw_jac * lexical_jaccard + nw_seq * sequence_score

        # Continuous mathematical gating: exactly 0 if intersection is empty
        gate = float(bool(intersection))
        return min(1.0, max(activation, lexical_jaccard)) * gate

    def _semantic_graph_neighbors(
        self,
        seed_item_ids: tuple[str, ...],
        *,
        limit: int,
    ) -> dict[str, list[str]]:
        graph_db = db_paths(self.home).graph
        if not (seed_item_ids and graph_db.exists()):
            return {}
        return self._do_graph_neighbors(graph_db, seed_item_ids, limit=limit)

    def _do_graph_neighbors(
        self,
        graph_db: Path,
        seed_item_ids: tuple[str, ...],
        *,
        limit: int,
    ) -> dict[str, list[str]]:
        cutoff_mult = self.get_parameter("graph_hub_degree_cutoff_multiplier", 3.0)
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
                return self._collect_graph_neighbors(
                    conn, seed_item_ids, limit=limit, cutoff_mult=cutoff_mult
                )
        except Exception:
            logger.exception("semantic graph neighbor recall failed")
            raise

    def _collect_graph_neighbors(
        self,
        conn: sqlite3.Connection,
        seed_item_ids: tuple[str, ...],
        *,
        limit: int,
        cutoff_mult: float,
    ) -> dict[str, list[str]]:
        neighbors: dict[str, list[str]] = {}
        for seed_id in seed_item_ids:
            seed_node = conn.execute(
                """
                SELECT id FROM graph_nodes
                WHERE type = 'MemoryItem' AND name = ?
                """,
                (seed_id,),
            ).fetchone()
            if seed_node:
                self._expand_seed_node(
                    conn,
                    seed_id=seed_id,
                    graph_node_id=str(seed_node[0]),
                    limit=limit,
                    cutoff_mult=cutoff_mult,
                    neighbors=neighbors,
                )
        return neighbors

    def _expand_seed_node(
        self,
        conn: sqlite3.Connection,
        *,
        seed_id: str,
        graph_node_id: str,
        limit: int,
        cutoff_mult: float,
        neighbors: dict[str, list[str]],
    ) -> None:
        direct_rows = conn.execute(
            """
            SELECT g.name, e.relation,
                   CASE WHEN e.source_id = ? THEN 'out' ELSE 'in' END
            FROM graph_edges e
            JOIN graph_nodes g ON (
                (e.source_id = ? AND e.target_id = g.id)
                OR (e.target_id = ? AND e.source_id = g.id)
            )
            WHERE g.type = 'MemoryItem' AND g.name != ?
            ORDER BY e.updated_at DESC
            LIMIT ?
            """,
            (graph_node_id, graph_node_id, graph_node_id, seed_id, limit),
        ).fetchall()
        for other_memory_id, relation, direction in direct_rows:
            reason = f"semantic_graph_neighbor={direction}:{relation}:{seed_id}"
            reasons = neighbors.setdefault(str(other_memory_id), [])
            (reason not in reasons) and reasons.append(reason)

        hub_rows = conn.execute(
            """
            SELECT h.id, h.type, h.name,
                   (SELECT count(*) FROM graph_edges WHERE source_id = h.id OR target_id = h.id) AS hub_degree
            FROM graph_edges e
            JOIN graph_nodes h ON (
                (e.source_id = ? AND e.target_id = h.id)
                OR (e.target_id = ? AND e.source_id = h.id)
            )
            WHERE h.type != 'MemoryItem'
            ORDER BY e.updated_at DESC
            LIMIT ?
            """,
            (graph_node_id, graph_node_id, limit),
        ).fetchall()
        for hub_id, hub_type, hub_name, hub_degree in hub_rows:
            below_cutoff = hub_degree <= limit * cutoff_mult
            below_cutoff and self._expand_hub_node(
                conn,
                hub_id=hub_id,
                hub_type=hub_type,
                hub_name=hub_name,
                seed_id=seed_id,
                limit=limit,
                neighbors=neighbors,
            )

    @staticmethod
    def _expand_hub_node(
        conn: sqlite3.Connection,
        *,
        hub_id: str,
        hub_type: str,
        hub_name: str,
        seed_id: str,
        limit: int,
        neighbors: dict[str, list[str]],
    ) -> None:
        second_hop_rows = conn.execute(
            """
            SELECT g.name
            FROM graph_edges e
            JOIN graph_nodes g ON (
                (e.source_id = g.id AND e.target_id = ?)
                OR (e.target_id = g.id AND e.source_id = ?)
            )
            WHERE g.type = 'MemoryItem' AND g.name != ?
            ORDER BY e.updated_at DESC
            LIMIT ?
            """,
            (hub_id, hub_id, seed_id, max(1, limit // 2)),
        ).fetchall()
        for (second_memory_id,) in second_hop_rows:
            spreading_reason = f"semantic_graph_spreading={hub_type}:{hub_name}:{seed_id}"
            reasons = neighbors.setdefault(str(second_memory_id), [])
            (spreading_reason not in reasons) and reasons.append(spreading_reason)

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
        lines: list[str] = []
        for recall in recalls:
            item = recall.item
            verified = _format_verification_date(item.last_verified_at)
            reasons = ", ".join(recall.reasons)
            reason_line = f"\n  reasons: {reasons}" * int(bool(reasons))
            lines.append(
                f"- [type={item.type} scope={item.scope} confidence={item.confidence:.2f} "
                f"score={recall.score:.4f} verified={verified} id={item.id}] "
                f"{truncate_middle(item.content, ACTIVE_MEMORY_CONTEXT_LIMIT)}"
                f"{reason_line}"
            )
        return "\n".join(lines)

    def active_constraints(
        self,
        *,
        limit: int = 100,
        allowed_scopes: set[str] | frozenset[str] | None = None,
    ) -> list[MemoryItem]:
        """Return all active constraint-type memories, unconditionally.

        Durable must/must-not rules must survive context compression
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
        entries = [
            f"- [scope={item.scope} confidence={item.confidence:.2f} "
            f"source={item.source} verified={_format_verification_date(item.last_verified_at)} id={item.id}] {item.content}"
            for item in constraints
        ]
        header = ["Durable constraints (reloaded from governed memory; always in effect):"] * int(bool(entries))
        return "\n".join([*header, *entries])

    def render_working_memory(self, *, goal_store: Any = None, limit: int = 20) -> str:
        """Render a per-step working-memory snapshot for the planner.

        Projects the active goals (goal id, phase, run id, objective) so the
        planner sees a fresh snapshot of what it is currently working on
        every step, regardless of how much conversation history has been
        truncated or summarized.

        Returns "" when there is no goal store or no active goals.
        """
        from ..lifecycle import Phase as _GoalPhase  # local import to avoid cycle

        goals = []
        if goal_store is not None:
            goals = goal_store.list(limit=limit)
        active = [g for g in goals if g.phase == str(_GoalPhase.RUNNING)]
        entries = [
            f"- [goal_id={g.id} phase={g.phase} run_id={g.run_id}] "
            f"objective={g.objective}"
            for g in active
        ]
        header = [
            "Working memory snapshot (reloaded every step; survives context compression):"
        ] * int(bool(entries))
        return "\n".join([*header, *entries])

    # --------------------------------------------------------------- sessions

    def new_session_id(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]

    def create_session(self, *, alias: str | None = None) -> str:
        session_id = self.new_session_id()
        alias and self.set_session_alias(alias, session_id)
        return session_id

    def set_session_alias(self, alias: str, session_id: str) -> SessionAlias:
        now = time.time()
        existing = self.get_session_alias(alias)
        created_at = now
        if existing:
            created_at = existing.created_at
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

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        message_id: str = "",
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
        trace_id: str = "",
        run_id: str = "",
    ) -> None:
        self.provider.add_message(
            session_id,
            role,
            content,
            time.time(),
            message_id=message_id or uuid.uuid4().hex,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            trace_id=trace_id,
            run_id=run_id,
        )

    def list_sessions(self) -> list[str]:
        return self.provider.list_sessions()

    def get_messages(self, session_id: str, limit: int = 50) -> list[StoredMessage]:
        return self.provider.get_messages(session_id, limit)

    def get_messages_for_run(
        self,
        session_id: str,
        run_id: str,
        limit: int = 50,
    ) -> list[StoredMessage]:
        return self.provider.get_messages_for_run(session_id, run_id, limit)

    def search_messages(
        self,
        query: str,
        *,
        limit: int = 8,
        session_id: str = "",
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
    ) -> list[tuple[StoredMessage, float, list[str]]]:
        return self.provider.search_messages_fts(
            query,
            limit,
            session_id=session_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
        )

    def clear_messages(self, session_id: str) -> int:
        """Delete all messages for *session_id*.

        conversation context is polluted with failing assumptions.
        context when the loop triggers ``REFLECT_AND_REPLAN``. The next
        planner call rebuilds context from durable constraints + working
        memory snapshot only. Returns the number of deleted rows.
        """
        return self.provider.clear_messages(session_id)

    def enqueue_consolidation(
        self,
        *,
        session_id: str,
        run_id: str,
        source: str,
        peer_id: str,
        sender_id: str,
    ) -> str:
        job_id = uuid.uuid4().hex
        now = time.time()
        with connect(db_paths(self.home).memory) as conn:
            inserted = conn.execute(
                """
                INSERT INTO memory_consolidation_jobs(
                    id, session_id, run_id, source, peer_id, sender_id, status,
                    owner, lease_expires_at, attempts, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', '', 0, 0, '', ?, ?)
                ON CONFLICT(session_id, run_id) DO NOTHING
                """,
                (job_id, session_id, run_id, source, peer_id, sender_id, now, now),
            )
            bool(inserted.rowcount) and _record_job_event(
                conn,
                job_id,
                event="enqueued",
                from_status="",
                to_status="pending",
                reason="conversation_turn_completed",
                created_at=now,
            )
            row = conn.execute(
                "SELECT id FROM memory_consolidation_jobs WHERE session_id = ? AND run_id = ?",
                (session_id, run_id),
            ).fetchone()
        if row:
            return str(row[0])
        return job_id

    def enqueue_unconsolidated_episodes(self, limit: int = 20) -> list[str]:
        """System consolidation sweep: discover past episodic conversations that have not yet
        been consolidated into semantic memory, and enqueue them for consolidation.
        Analogous to biological hippocampal replay during sleep/idle periods.
        """
        enqueued_job_ids: list[str] = []
        with connect(db_paths(self.home).memory) as conn:
            rows = conn.execute(
                """
                SELECT session_id, source, peer_id, sender_id, run_id
                FROM messages m
                WHERE NOT EXISTS (
                    SELECT 1 FROM memory_consolidation_jobs j
                    WHERE j.session_id = m.session_id
                )
                GROUP BY session_id
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            for session_id, source, peer_id, sender_id, run_id in rows:
                job_id = self.enqueue_consolidation(
                    session_id=str(session_id),
                    run_id=str(run_id or session_id),
                    source=str(source or "conversation"),
                    peer_id=str(peer_id or ""),
                    sender_id=str(sender_id or ""),
                )
                enqueued_job_ids.append(job_id)
        return enqueued_job_ids

    def list_consolidation_jobs(
        self,
        *,
        job_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[MemoryConsolidationJob]:
        normalized_job_id = str(job_id or "").strip()
        normalized_status = str(status or "").strip()
        params: list[Any] = []
        predicates: list[str] = []
        filters = (
            (normalized_job_id, "id = ?", normalized_job_id),
            (normalized_status, "status = ?", normalized_status),
        )
        for active, clause, val in filters:
            active and predicates.append(clause)
            active and params.append(val)
        where = _where_clause(predicates)
        params.append(max(1, min(int(limit), 500)))
        with connect(db_paths(self.home).memory) as conn:
            rows = conn.execute(
                f"""
                SELECT {_CONSOLIDATION_JOB_COLUMNS}
                FROM memory_consolidation_jobs
                {where}
                ORDER BY updated_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [MemoryConsolidationJob(*row) for row in rows]

    def retry_consolidation_jobs(
        self,
        job_ids: list[str],
        *,
        reason: str,
        now: float | None = None,
    ) -> list[MemoryConsolidationJob]:
        ids = list(dict.fromkeys(str(item).strip() for item in job_ids if str(item).strip()))
        (not ids) and _raise(ValueError("memory consolidation retry requires job_ids"))
        normalized_reason = str(reason or "").strip()
        (not normalized_reason) and _raise(ValueError("memory consolidation retry requires reason"))
        current_time = _resolve_now(now)
        retried: list[str] = []
        with connect(db_paths(self.home).memory) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for job_id in ids:
                row = conn.execute(
                    "SELECT status, error FROM memory_consolidation_jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                is_dead = row is not None and str(row[0]) == "dead_letter"
                previous_error = ""
                if is_dead:
                    previous_error = str(row[1] or "")
                updated = is_dead and conn.execute(
                    """
                    UPDATE memory_consolidation_jobs
                    SET status = 'pending', owner = '', lease_expires_at = 0,
                        error = '', updated_at = ?
                    WHERE id = ? AND status = 'dead_letter'
                    """,
                    (current_time, job_id),
                )
                was_updated = bool(updated and updated.rowcount)
                was_updated and _record_job_event(
                    conn,
                    job_id,
                    event="retry_requested",
                    from_status="dead_letter",
                    to_status="pending",
                    reason=normalized_reason,
                    error=previous_error,
                    created_at=current_time,
                )
                was_updated and retried.append(job_id)
            if not retried:
                return []
            return self._fetch_retried_jobs(conn, retried)

    @staticmethod
    def _fetch_retried_jobs(conn: sqlite3.Connection, retried: list[str]) -> list[MemoryConsolidationJob]:
        placeholders = ", ".join("?" for _ in retried)
        rows = conn.execute(
            f"""
            SELECT {_CONSOLIDATION_JOB_COLUMNS}
            FROM memory_consolidation_jobs WHERE id IN ({placeholders})
            ORDER BY updated_at DESC
            """,
            retried,
        ).fetchall()
        return [MemoryConsolidationJob(*row) for row in rows]

    def list_consolidation_job_events(self, job_id: str) -> list[dict[str, Any]]:
        with connect(db_paths(self.home).memory) as conn:
            rows = conn.execute(
                """
                SELECT id, event, from_status, to_status, reason, error, created_at
                FROM memory_consolidation_job_events
                WHERE job_id = ? ORDER BY id
                """,
                (job_id,),
            ).fetchall()
        return [
            {
                "id": int(row[0]),
                "job_id": job_id,
                "event": str(row[1]),
                "from_status": str(row[2]),
                "to_status": str(row[3]),
                "reason": str(row[4]),
                "error": str(row[5]),
                "created_at": float(row[6]),
            }
            for row in rows
        ]

    def claim_consolidation_jobs(
        self,
        *,
        owner: str,
        limit: int = 10,
        lease_seconds: float = 300.0,
        now: float | None = None,
    ) -> list[MemoryConsolidationJob]:
        current_time = _resolve_now(now)
        effective_lease = lease_seconds
        if lease_seconds == 300.0:
            effective_lease = self.get_parameter("consolidation_lease_seconds", 300.0)
        with connect(db_paths(self.home).memory) as conn:
            conn.execute("BEGIN IMMEDIATE")
            legacy_failed = conn.execute(
                "SELECT id, error FROM memory_consolidation_jobs WHERE status = 'failed'"
            ).fetchall()
            for job_id, error in legacy_failed:
                conn.execute(
                    """
                    UPDATE memory_consolidation_jobs
                    SET status = 'dead_letter', owner = '', lease_expires_at = 0,
                        updated_at = ?
                    WHERE id = ? AND status = 'failed'
                    """,
                    (current_time, job_id),
                )
                _record_job_event(
                    conn,
                    str(job_id),
                    event="legacy_failed_normalized",
                    from_status="failed",
                    to_status="dead_letter",
                    reason="legacy_status_normalization",
                    error=str(error or ""),
                    created_at=current_time,
                )
            selected = conn.execute(
                """
                SELECT id, status FROM memory_consolidation_jobs
                WHERE status = 'pending'
                   OR (status = 'active' AND lease_expires_at <= ?)
                ORDER BY updated_at ASC LIMIT ?
                """,
                (current_time, max(1, limit)),
            ).fetchall()
            ids = [str(row[0]) for row in selected]
            for job_id, previous_status in selected:
                conn.execute(
                    """
                    UPDATE memory_consolidation_jobs
                    SET status = 'active', owner = ?, lease_expires_at = ?,
                        attempts = attempts + 1, error = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        owner,
                        current_time + effective_lease,
                        current_time,
                        job_id,
                    ),
                )
                _record_job_event(
                    conn,
                    str(job_id),
                    event="claimed",
                    from_status=str(previous_status),
                    to_status="active",
                    reason={"active": "lease_reclaimed"}.get(str(previous_status), "worker_claim"),
                    created_at=current_time,
                )
            if not ids:
                return []
            return self._fetch_claimed_jobs(conn, ids)

    @staticmethod
    def _fetch_claimed_jobs(conn: sqlite3.Connection, ids: list[str]) -> list[MemoryConsolidationJob]:
        placeholders = ", ".join("?" for _ in ids)
        rows = conn.execute(
            f"""
            SELECT {_CONSOLIDATION_JOB_COLUMNS}
            FROM memory_consolidation_jobs WHERE id IN ({placeholders})
            ORDER BY updated_at ASC
            """,
            ids,
        ).fetchall()
        return [MemoryConsolidationJob(*row) for row in rows]

    async def consolidate_job(self, job: MemoryConsolidationJob, runtime: Any) -> list[MemoryItem]:
        from ..prompt_os import assemble_memory_consolidation_messages
        from ..prompting import PromptLayerStore
        from .scopes import default_memory_scope

        messages = self.get_messages_for_run(job.session_id, job.run_id, limit=50) or self.get_messages(job.session_id, limit=50)
        if not messages:
            self._finish_consolidation_job(job, status="completed")
            return []
        return await self._run_consolidation(job, runtime, messages)

    async def _run_consolidation(
        self,
        job: MemoryConsolidationJob,
        runtime: Any,
        messages: list[StoredMessage],
    ) -> list[MemoryItem]:
        from ..prompt_os import assemble_memory_consolidation_messages
        from ..prompting import PromptLayerStore
        from .scopes import default_memory_scope

        transcript = [
            {"role": message.role, "content": message.content}
            for message in messages
        ]
        active_scope = default_memory_scope(
            source=job.source,
            peer_id=job.peer_id,
            sender_id=job.sender_id,
            session_id=job.session_id,
            workspace="",
            home=self.home,
        )
        active_items = [
            item
            for item in self._list_active_learnable_items()
            if item.scope == active_scope
        ]
        output_schema = {
            "name": "memory_consolidation",
            "strict": False,
            "schema": {
                "type": "object",
                "properties": {
                    "learnings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": ["add", "revoke"]},
                                "id": {"type": "string"},
                                "type": {"type": "string", "enum": list(LEARNABLE_MEMORY_TYPES)},
                                "content": {"type": "string"},
                                "confidence": {"type": "number"},
                                "reason": {"type": "string"},
                                "contradicts": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["action", "type", "content"],
                        },
                    }
                },
                "required": ["learnings"],
                "additionalProperties": False,
            },
        }
        try:
            response = await runtime.provider.complete_for(
                "consolidator",
                assemble_memory_consolidation_messages(
                    task_prompt=PromptLayerStore(self.home).read("task_memory_consolidator"),
                    transcript=transcript,
                    active_memory=[item.__dict__ for item in active_items],
                    scope=active_scope,
                ),
                output_schema=output_schema,
            )
            data = json.loads(response)
            learnings = []
            if isinstance(data, dict):
                learnings = _as_list(data.get("learnings"))
            affected = self._apply_learnings(
                learnings,
                active_items,
                source=job.source or "conversation",
                provenance=f"memory-job:{job.id}:run:{job.run_id}",
                ledger_run_id=job.run_id,
                default_add_reason="conversation consolidation",
                scope=active_scope,
            )
        except Exception as exc:
            self._finish_consolidation_job(
                job,
                status="dead_letter",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        self._finish_consolidation_job(job, status="completed")
        return affected

    def _finish_consolidation_job(
        self,
        job: MemoryConsolidationJob,
        *,
        status: str,
        error: str = "",
    ) -> None:
        with connect(db_paths(self.home).memory) as conn:
            updated_at = time.time()
            updated = conn.execute(
                """
                UPDATE memory_consolidation_jobs
                SET status = ?, owner = '', lease_expires_at = 0, error = ?, updated_at = ?
                WHERE id = ? AND owner = ?
                """,
                (status, error, updated_at, job.id, job.owner),
            )
            bool(updated.rowcount) and _record_job_event(
                conn,
                job.id,
                event=status,
                from_status=job.status,
                to_status=status,
                reason="worker_result",
                error=error,
                created_at=updated_at,
            )

    def _list_active_learnable_items(self) -> list[MemoryItem]:
        return [
            item
            for item in self.list_items(limit=ACTIVE_MEMORY_CONTEXT_LIMIT * len(LEARNABLE_MEMORY_TYPES))
            if item.type in LEARNABLE_MEMORY_TYPES and item.status in RECALLABLE_STATUSES
        ]

    # --------------------------------------------------------- apply learnings

    def _apply_learnings(
        self,
        learnings: list,
        active_items: list,
        *,
        source: str,
        provenance: str,
        ledger_run_id: str,
        default_add_reason: str,
        scope: str = "global",
    ) -> list:
        """Apply extracted add/revoke learnings with full provenance + ledger.

        Used by the governed memory-consolidation pipeline; the store only
        validates and persists model-declared learnings.
        """
        from ..evolution import EvolutionLedger

        ledger = EvolutionLedger(self.home)
        affected_items: list = []
        visible_item_ids = {item.id for item in active_items}
        existing_by_key: dict[tuple[str, str], MemoryItem] = {
            (item.type, item.content.strip().lower()): item
            for item in self.list_items(limit=1000)
            if item.scope == scope and item.status in RECALLABLE_STATUSES
        }
        def _apply_learning_add(learning: dict) -> None:
            item = self._apply_add_learning(
                learning,
                existing_by_key,
                visible_item_ids=visible_item_ids,
                source=source,
                provenance=provenance,
                default_add_reason=default_add_reason,
                scope=scope,
            )
            has_item = item is not None
            has_item and affected_items.append(item)
            has_item and ledger.record(
                run_id=ledger_run_id,
                target_type="memory_item",
                target_id=getattr(item, "id", ""),
                reason="memory_learning_added",
                before="",
                after=json.dumps(getattr(item, "__dict__", {}), default=str),
            )

        def _apply_learning_revoke(learning: dict) -> None:
            item_id = str(learning.get("id", "")).strip()
            is_visible = bool(item_id and item_id in visible_item_ids)
            old_item = is_visible and self.get_item(item_id)
            can_revoke = bool(
                old_item
                and old_item.scope == scope
                and old_item.status in RECALLABLE_STATUSES
            )
            updated_item = can_revoke and self.set_status(item_id, "revoked")
            bool(updated_item) and affected_items.append(updated_item)
            can_revoke and ledger.record(
                run_id=ledger_run_id,
                target_type="memory_item",
                target_id=item_id,
                reason=str(learning.get("reason") or "memory_learning_revoked"),
                before=json.dumps(old_item.__dict__, default=str),
                after="revoked",
            )
            # Dynamic parameter adjustment on revocation (error signal)
            can_revoke and self.set_parameter(
                "decay_base_delta",
                round(min(0.20, self.get_parameter("decay_base_delta", 0.05) + 0.005), 4),
                reason="learning_revocation_dynamic_tuning",
            )

        learning_action_handlers = {
            "add": _apply_learning_add,
            "revoke": _apply_learning_revoke,
        }
        for learning in learnings:
            if not isinstance(learning, dict):
                continue
            action = str(learning.get("action", "")).strip().lower()
            handler = learning_action_handlers.get(action, lambda _: None)
            handler(learning)
        return affected_items

    def _apply_add_learning(
        self,
        learning: dict,
        existing_by_key: dict[tuple[str, str], MemoryItem],
        *,
        visible_item_ids: set,
        source: str,
        provenance: str,
        default_add_reason: str,
        scope: str = "global",
    ):
        m_type = str(learning.get("type", "")).strip().lower()
        content = str(learning.get("content", "")).strip()
        is_valid_type = bool(content and m_type in LEARNABLE_MEMORY_TYPES)
        memory_key = (m_type, content.lower())
        if not (is_valid_type and (memory_key not in existing_by_key)):
            return None
        return self._insert_add_learning(
            learning,
            existing_by_key,
            memory_key=memory_key,
            m_type=m_type,
            content=content,
            visible_item_ids=visible_item_ids,
            source=source,
            provenance=provenance,
            default_add_reason=default_add_reason,
            scope=scope,
        )

    def _insert_add_learning(
        self,
        learning: dict,
        existing_by_key: dict[tuple[str, str], MemoryItem],
        *,
        memory_key: tuple[str, str],
        m_type: str,
        content: str,
        visible_item_ids: set,
        source: str,
        provenance: str,
        default_add_reason: str,
        scope: str,
    ) -> MemoryItem:
        default_conf = self.get_parameter("consolidation_default_confidence", 0.70)
        try:
            conf_val = float(learning.get("confidence", default_conf))
        except (ValueError, TypeError):
            conf_val = default_conf

        # Dynamic EMA adaptation of consolidation confidence based on model output
        alpha = 0.05
        ema_conf = round((1.0 - alpha) * default_conf + alpha * conf_val, 4)
        self.set_parameter("consolidation_default_confidence", ema_conf, reason="consolidation_model_ema")

        contradicts_list = _as_list(learning.get("contradicts", []))
        contradicts = [str(item_id) for item_id in contradicts_list if str(item_id) in visible_item_ids]
        promoted_status = "proposed"
        metadata = {}
        if contradicts:
            metadata = {"contradicts": contradicts}
        new_item = self.add_item(
            memory_type=m_type,
            content=content,
            source=source,
            scope=scope,
            status=promoted_status,
            confidence=conf_val,
            metadata=metadata,
            reason=str(learning.get("reason") or default_add_reason),
            provenance=provenance,
        )
        existing_by_key[memory_key] = new_item
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
        has_related = bool(related)
        unresolved = [c for c in related if c.status == "unresolved"]
        conflict_type = ("unresolved" * bool(unresolved)) or "declared"
        conflict_count = len(unresolved) or len(related)
        conflict_reasons = [f"{conflict_type}_memory_conflicts={conflict_count}"] * int(has_related)
        return MemoryRecall(
            item=recall.item,
            score=recall.score,
            reasons=[*recall.reasons, *conflict_reasons],
            conflicts=related,
        )


def _blocking_hook(decisions: list[HookDecision]) -> HookDecision | None:
    return next((decision for decision in decisions if decision.decision == "block"), None)


def _clean_str_item(val: str) -> list[str]:
    s = val.strip()
    if s:
        return [s]
    return []


_METADATA_ID_EXTRACTORS: dict[type, Any] = {
    str: _clean_str_item,
    list: lambda val: [str(item).strip() for item in val if str(item).strip()],
}


def _metadata_id_list(value: Any) -> list[str]:
    extractor = _METADATA_ID_EXTRACTORS.get(type(value), lambda _: [])
    return extractor(value)


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


def _text_features(text: str) -> set[str]:
    normalized = text.strip().lower()
    words = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    chunks = re.findall(r"[\u4e00-\u9fff]+", normalized)
    cjk_grams = {
        chunk[i : i + 2]
        for chunk in chunks
        for i in range(max(1, len(chunk) - 1))
    }
    return words | cjk_grams


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


_CONFLICT_STATUS_MATRIX: dict[tuple[bool, bool, bool], str] = {
    (False, False, False): "missing_target",
    (False, True, False): "missing_target",
    (True, True, True): "unresolved",
    (True, True, False): "resolved",
    (True, False, True): "inactive",
    (True, False, False): "inactive",
}


def _memory_conflict_status(item: MemoryItem, conflicting_item: MemoryItem | None) -> str:
    has_target = conflicting_item is not None
    item_active = item.status in ACTIVE_STATUSES
    target_active = has_target and (conflicting_item.status in ACTIVE_STATUSES)
    return _CONFLICT_STATUS_MATRIX.get((has_target, item_active, target_active), "inactive")
