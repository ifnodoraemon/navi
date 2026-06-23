"""Core tool handlers."""
from __future__ import annotations
import os
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse
from typing import Any
from ..config import load_config
from ..fact_tools import service_facts, run_facts
from ..hooks import HookRegistry
from ..memory import MemoryStore
from ..operating_context import permission_allows
from ..runs import Approval
from ..safeguards import capability_safeguard_facts
from ..skills import SkillStore
from ..tools import ALL_EXECUTION_CONTEXTS, ToolRegistry, ToolResult, ToolSpec

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
        items = MemoryStore(home).list_items(memory_type=memory_type, status=status, limit=limit)
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
    recalls = store.recall(query, limit=limit, goal=goal)
    return ToolResult(
        tool="memory.recall",
        ok=True,
        facts={
            "query": query,
            "goal": goal,
            "items": [_memory_recall_facts(recall) for recall in recalls],
            "count": len(recalls),
            "limit": limit,
            "rendered": store.render_context(query, limit=limit, goal=goal),
        },
    )


def _memory_conflicts(home: Path, args: dict[str, Any]) -> ToolResult:
    limit = _positive_int(args.get("limit"), default=20, maximum=100)
    conflicts = MemoryStore(home).list_conflicts(limit=limit)
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


