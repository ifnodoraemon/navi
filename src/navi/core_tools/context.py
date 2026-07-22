"""Deterministic context evidence search for model-directed recall."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..memory import MemoryRecall, MemoryStore, StoredMessage
from ..text_utils import truncate_middle
from ..tools import ToolResult
from .memory import _memory_conflict_facts, _memory_item_facts
from .utils import _positive_int

CONTEXT_EVIDENCE_MAX_ITEMS = 20
CONTEXT_EVIDENCE_EXCERPT_CHARS = 700
CONTEXT_RECENT_MESSAGE_LIMIT = 6


def _context_search(home: Path, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    terms = _string_list(args.get("terms"))
    if not query and terms:
        query = " ".join(terms)
    need = _string_list(args.get("need"))
    max_items = _positive_int(
        args.get("max_items"),
        default=8,
        maximum=CONTEXT_EVIDENCE_MAX_ITEMS,
    )
    context = _context_args(args)
    allowed_scopes = _allowed_scopes(args)
    store = MemoryStore(home)

    evidence: dict[str, dict[str, Any]] = {}
    if context.get("session_id"):
        recent_base = 950.0 if not query else 620.0
        recent = store.get_messages(
            str(context["session_id"]),
            limit=min(CONTEXT_RECENT_MESSAGE_LIMIT, max_items),
        )
        for offset, message in enumerate(reversed(recent)):
            _put_evidence(
                evidence,
                _message_evidence(
                    message,
                    score=recent_base - offset,
                    reasons=["same_session_recent"],
                ),
            )

    search_queries = _search_queries(query, terms)
    if search_queries:
        for search_query in search_queries:
            message_matches = store.search_messages(
                search_query,
                limit=max_items * 2,
                session_id=str(context.get("session_id") or ""),
                source=str(context.get("source") or ""),
                peer_id=str(context.get("peer_id") or ""),
                sender_id=str(context.get("sender_id") or ""),
            )
            for message, rank, reasons in message_matches:
                _put_evidence(
                    evidence,
                    _message_evidence(
                        message,
                        score=max(0.0, 900.0 - abs(rank)),
                        reasons=[*reasons, f"search_query={search_query}"],
                    ),
                )
        for search_query in search_queries:
            recalls = store.recall(
                search_query,
                limit=max_items,
                allowed_scopes=allowed_scopes,
            )
            for recall in recalls:
                _put_evidence(evidence, _memory_evidence(recall, search_query=search_query))

    ordered = sorted(
        evidence.values(),
        key=lambda item: (
            -float(item.get("score") or 0.0),
            -float(item.get("created_at") or 0.0),
            str(item.get("evidence_id") or ""),
        ),
    )[:max_items]

    return ToolResult(
        tool="context.search",
        ok=True,
        facts={
            "policy": "deterministic_context_search_v1",
            "query": query,
            "terms": terms,
            "need": need,
            "time_hint": str(args.get("time_hint") or "").strip(),
            "scope_hint": str(args.get("scope_hint") or "").strip(),
            "identity": context,
            "allowed_scopes": sorted(allowed_scopes) if allowed_scopes is not None else [],
            "evidence": ordered,
            "evidence_ids": [str(item["evidence_id"]) for item in ordered],
            "count": len(ordered),
            "limit": max_items,
            "selection_policy": (
                "deterministic only: same-session recent messages, "
                "FTS/exact conversation matches, and governed memory recall"
            ),
            "model_decides_usage": True,
        },
    )


def _message_evidence(
    message: StoredMessage,
    *,
    score: float,
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "evidence_id": f"msg:{message.message_id}",
        "kind": "conversation_message",
        "scope": "session",
        "session_id": message.session_id,
        "source": message.source,
        "peer_id": message.peer_id,
        "sender_id": message.sender_id,
        "trace_id": message.trace_id,
        "run_id": message.run_id,
        "role": message.role,
        "created_at": message.created_at,
        "content": truncate_middle(message.content, CONTEXT_EVIDENCE_EXCERPT_CHARS),
        "score": score,
        "rank_reasons": list(dict.fromkeys(reasons)),
        "trust": "conversation_log",
        "stale": False,
        "conflicts": [],
    }


def _memory_evidence(recall: MemoryRecall, *, search_query: str) -> dict[str, Any]:
    item = recall.item
    return {
        "evidence_id": f"mem:{item.id}",
        "kind": "memory_item",
        "scope": item.scope,
        "source": item.source,
        "peer_id": "",
        "sender_id": "",
        "session_id": "",
        "trace_id": "",
        "run_id": "",
        "role": "memory",
        "created_at": item.created_at,
        "content": truncate_middle(item.content, CONTEXT_EVIDENCE_EXCERPT_CHARS),
        "score": max(0.0, 800.0 - abs(float(recall.score))),
        "rank_reasons": list(
            dict.fromkeys(["memory_recall", f"search_query={search_query}", *recall.reasons])
        ),
        "trust": "governed_memory",
        "stale": item.status not in {"active", "accepted"},
        "item": _memory_item_facts(item),
        "conflicts": [_memory_conflict_facts(conflict) for conflict in recall.conflicts],
    }


def _put_evidence(target: dict[str, dict[str, Any]], item: dict[str, Any]) -> None:
    evidence_id = str(item.get("evidence_id") or "").strip()
    if not evidence_id:
        return
    existing = target.get(evidence_id)
    if existing is None:
        target[evidence_id] = item
        return
    existing["score"] = max(float(existing.get("score") or 0.0), float(item.get("score") or 0.0))
    reasons = list(existing.get("rank_reasons") or []) + list(item.get("rank_reasons") or [])
    existing["rank_reasons"] = list(dict.fromkeys(str(reason) for reason in reasons if reason))


def _context_args(args: dict[str, Any]) -> dict[str, str]:
    raw = args.get("_context")
    raw_context = raw if isinstance(raw, dict) else {}
    return {
        "source": str(raw_context.get("source") or "").strip(),
        "peer_id": str(raw_context.get("peer_id") or "").strip(),
        "sender_id": str(raw_context.get("sender_id") or "").strip(),
        "session_id": str(raw_context.get("session_id") or "").strip(),
        "workspace": str(raw_context.get("workspace") or "").strip(),
        "trace_id": str(raw_context.get("trace_id") or "").strip(),
    }


def _allowed_scopes(args: dict[str, Any]) -> set[str] | None:
    raw = args.get("_allowed_scopes")
    if not isinstance(raw, list):
        return None
    return {str(item).strip() for item in raw if str(item).strip()}


def _string_list(raw: object) -> list[str]:
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _search_queries(query: str, terms: list[str]) -> list[str]:
    queries: list[str] = []
    for candidate in [query, *terms]:
        candidate = candidate.strip()
        if candidate and candidate not in queries:
            queries.append(candidate)
    return queries
