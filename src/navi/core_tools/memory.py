"""Core tool handlers."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from ..memory import MemoryStore
from .utils import _positive_int
from ..tools import ToolResult

def _memory_item_facts(item) -> dict[str, Any]:
    return {
        "id": item.id,
        "type": item.type,
        "status": item.status,
        "scope": item.scope,
        "content": item.content,
        "source": item.source,
        "confidence": item.confidence,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "last_verified_at": item.last_verified_at,
        "expires_at": item.expires_at,
        "metadata": item.metadata,
        "reason": getattr(item, "reason", ""),
        "provenance": getattr(item, "provenance", ""),
    }


def _memory_recall_facts(recall) -> dict[str, Any]:
    facts = _memory_item_facts(recall.item)
    facts["score"] = recall.score
    facts["reasons"] = list(recall.reasons)
    facts["conflicts"] = [_memory_conflict_facts(conflict) for conflict in recall.conflicts]
    return facts


def _memory_conflict_facts(conflict) -> dict[str, Any]:
    return {
        "item": _memory_item_facts(conflict.item),
        "relation": conflict.relation,
        "conflicting_item_id": conflict.conflicting_item_id,
        "conflicting_item": _memory_item_facts(conflict.conflicting_item)
        if conflict.conflicting_item
        else None,
        "status": conflict.status,
        "reason": conflict.reason,
    }


def _memory_list(home: Path, args: dict[str, Any]) -> ToolResult:
    limit = _positive_int(args.get("limit"), default=20, maximum=100)
    memory_type = str(args.get("type") or "").strip().lower() or None
    status = str(args.get("status") or "").strip().lower() or None
    try:
        items = MemoryStore(home).list_items(
            memory_type=memory_type,
            status=status,
            allowed_scopes=_allowed_scopes(args),
            limit=limit,
        )
    except ValueError as exc:
        return ToolResult(tool="memory.list", ok=False, error=str(exc))
    return ToolResult(
        tool="memory.list",
        ok=True,
        facts={
            "items": [_memory_item_facts(item) for item in items],
            "count": len(items),
            "limit": limit,
            "type": memory_type or "",
            "status": status or "",
        },
    )


def _memory_recall(home: Path, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(tool="memory.recall", ok=False, error="query is required")
    goal = str(args.get("goal") or "").strip()
    limit = _positive_int(args.get("limit"), default=8, maximum=50)
    store = MemoryStore(home)
    allowed_scopes = _allowed_scopes(args)
    recalls = store.recall(
        query,
        limit=limit,
        goal=goal,
        allowed_scopes=allowed_scopes,
    )
    return ToolResult(
        tool="memory.recall",
        ok=True,
        facts={
            "query": query,
            "goal": goal,
            "items": [_memory_recall_facts(recall) for recall in recalls],
            "activation_candidate_ids": [recall.item.id for recall in recalls],
            "count": len(recalls),
            "limit": limit,
            "rendered": store.render_context(
                query,
                limit=limit,
                goal=goal,
                allowed_scopes=allowed_scopes,
            ),
        },
    )


def _allowed_scopes(args: dict[str, Any]) -> set[str] | None:
    raw = args.get("_allowed_scopes")
    if not isinstance(raw, list):
        return None
    return {str(item).strip() for item in raw if str(item).strip()}


def _memory_record_activation(home: Path, args: dict[str, Any]) -> ToolResult:
    raw_ids = args.get("item_ids")
    if isinstance(raw_ids, str):
        item_ids = [raw_ids.strip()] if raw_ids.strip() else []
    elif isinstance(raw_ids, list):
        item_ids = [str(item).strip() for item in raw_ids if str(item).strip()]
    else:
        item_ids = []
    if not item_ids:
        return ToolResult(tool="memory.record_activation", ok=False, error="item_ids is required")
    reason = str(args.get("reason") or "").strip()
    provenance = str(args.get("provenance") or "").strip()
    if not reason:
        return ToolResult(tool="memory.record_activation", ok=False, error="reason is required")
    if not provenance:
        return ToolResult(tool="memory.record_activation", ok=False, error="provenance is required")
    store = MemoryStore(home)
    allowed_scopes = _allowed_scopes(args)
    activated = []
    missing = []
    try:
        for item_id in item_ids:
            current = store.get_item(item_id)
            if current is None or (
                allowed_scopes is not None and current.scope not in allowed_scopes
            ):
                missing.append(item_id)
                continue
            item = store.record_activation(
                item_id,
                reason=reason,
                provenance=provenance,
            )
            if item is None:
                missing.append(item_id)
            else:
                activated.append(_memory_item_facts(item))
    except ValueError as exc:
        return ToolResult(tool="memory.record_activation", ok=False, error=str(exc))
    return ToolResult(
        tool="memory.record_activation",
        ok=True,
        facts={
            "entity_type": "memory_activation",
            "entity_id": ",".join(item_ids),
            "state_transition": "recorded",
            "turn_scope": "current",
            "activated_items": activated,
            "activated_count": len(activated),
            "missing_item_ids": missing,
            "missing_count": len(missing),
            "reason": reason,
            "provenance": provenance,
        },
    )


def _memory_conflicts(home: Path, args: dict[str, Any]) -> ToolResult:
    limit = _positive_int(args.get("limit"), default=20, maximum=100)
    conflicts = MemoryStore(home).list_conflicts(
        limit=limit,
        allowed_scopes=_allowed_scopes(args),
    )
    return ToolResult(
        tool="memory.conflicts",
        ok=True,
        facts={
            "conflicts": [_memory_conflict_facts(conflict) for conflict in conflicts],
            "count": len(conflicts),
            "limit": limit,
            "unresolved_count": len(
                [conflict for conflict in conflicts if conflict.status == "unresolved"]
            ),
        },
    )
