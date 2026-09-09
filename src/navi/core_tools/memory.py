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
    return (
        ToolResult(tool="memory.recall", ok=False, error="query is required")
        if not query
        else _execute_memory_recall(home, args, query)
    )


def _execute_memory_recall(home: Path, args: dict[str, Any], query: str) -> ToolResult:
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
    return {str(item).strip() for item in raw if str(item).strip()} if isinstance(raw, list) else None


def _memory_record_activation(home: Path, args: dict[str, Any]) -> ToolResult:
    raw_ids = args.get("item_ids")
    raw_list = [raw_ids] if isinstance(raw_ids, str) else (raw_ids if isinstance(raw_ids, list) else [])
    item_ids = [str(item).strip() for item in raw_list if str(item).strip()]
    reason = str(args.get("reason") or "").strip()
    provenance = str(args.get("provenance") or "").strip()
    validation_error = (
        ("item_ids is required" * int(not item_ids))
        or ("reason is required" * int(not reason))
        or ("provenance is required" * int(not provenance))
    )
    return (
        ToolResult(tool="memory.record_activation", ok=False, error=validation_error)
        if validation_error
        else _execute_memory_record_activation(
            home,
            item_ids=item_ids,
            reason=reason,
            provenance=provenance,
            args=args,
        )
    )


def _execute_memory_record_activation(
    home: Path,
    *,
    item_ids: list[str],
    reason: str,
    provenance: str,
    args: dict[str, Any],
) -> ToolResult:
    store = MemoryStore(home)
    allowed_scopes = _allowed_scopes(args)
    activated = []
    missing = []
    query = str(args.get("query") or "").strip() or None
    try:
        for item_id in item_ids:
            current = store.get_item(item_id)
            scope_ok = current is not None and (allowed_scopes is None or current.scope in allowed_scopes)
            activated_item = scope_ok and store.record_activation(
                item_id,
                reason=reason,
                provenance=provenance,
                query=query,
            )
            has_activated = bool(activated_item)
            has_activated and activated.append(_memory_item_facts(activated_item))
            (not has_activated) and missing.append(item_id)
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


def _param_get(store: MemoryStore, args: dict[str, Any]) -> ToolResult:
    name = str(args.get("name") or "").strip()
    entry = store.get_parameter_entry(name)
    ok = bool(name and entry)
    error = f"unknown memory parameter: {name}" if (name and not entry) else "name is required for get action"
    return ToolResult(
        tool="memory.parameters",
        ok=ok,
        error=None if ok else error,
        facts={"action": "get", "parameter": entry} if ok else {},
    )


def _param_set(store: MemoryStore, args: dict[str, Any]) -> ToolResult:
    name = str(args.get("name") or "").strip()
    val = float(args.get("value", 0.0))
    reason = str(args.get("reason") or "explicit_tool_update").strip()
    ok = bool(name and "value" in args)
    error = "name and value are required for set action" if not ok else None
    _ = store.set_parameter(name, val, reason=reason) if ok else None
    entry = store.get_parameter_entry(name) if ok else None
    return ToolResult(
        tool="memory.parameters",
        ok=ok,
        error=error,
        facts={"action": "set", "parameter": entry} if ok else {},
    )


def _param_list(store: MemoryStore, _args: dict[str, Any]) -> ToolResult:
    params = store.list_parameters()
    return ToolResult(
        tool="memory.parameters",
        ok=True,
        facts={"action": "list", "parameters": params, "count": len(params)},
    )


_PARAM_ACTIONS = {
    "get": _param_get,
    "set": _param_set,
    "list": _param_list,
}


def _memory_parameters(home: Path, args: dict[str, Any]) -> ToolResult:
    action = str(args.get("action") or "list").strip().lower()
    store = MemoryStore(home)
    handler = _PARAM_ACTIONS.get(action, _param_list)
    return handler(store, args)

