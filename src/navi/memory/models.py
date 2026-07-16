"""Memory domain dataclasses and policy facts."""

from __future__ import annotations

from dataclasses import dataclass

from ..specs_data import MEMORY_POLICY_SPEC

_MEMORY_POLICY = MEMORY_POLICY_SPEC
MEMORY_TYPES = {str(item) for item in _MEMORY_POLICY["types"]}
LEARNABLE_MEMORY_TYPES = tuple(str(item) for item in _MEMORY_POLICY["learnable_types"])
MEMORY_STATUSES = {str(item) for item in _MEMORY_POLICY["statuses"]}
ACTIVE_STATUSES = {str(item) for item in _MEMORY_POLICY["active_statuses"]}
ACTIVE_MEMORY_CONTEXT_LIMIT = int(_MEMORY_POLICY["active_memory_context_limit"])
TASK_LEARNING_LOG_LIMIT = int(_MEMORY_POLICY["task_learning_log_limit"])
TYPE_PRIORITY = {str(key): int(value) for key, value in _MEMORY_POLICY["type_priority"].items()}


@dataclass(frozen=True)
class StoredMessage:
    session_id: str
    role: str
    content: str
    created_at: float


@dataclass(frozen=True)
class SessionAlias:
    alias: str
    session_id: str
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class MemoryItem:
    id: str
    type: str
    status: str
    scope: str
    content: str
    source: str
    confidence: float
    created_at: float
    updated_at: float
    last_verified_at: float
    expires_at: float
    metadata: dict
    reason: str = ""
    provenance: str = ""


@dataclass(frozen=True)
class MemoryConflict:
    item: MemoryItem
    relation: str
    conflicting_item_id: str
    conflicting_item: MemoryItem | None
    status: str
    reason: str


@dataclass(frozen=True)
class MemoryRecall:
    item: MemoryItem
    score: float
    reasons: list[str]
    conflicts: tuple[MemoryConflict, ...] = ()


def memory_policy_facts() -> dict:
    return {
        "types": sorted(MEMORY_TYPES),
        "learnable_types": list(LEARNABLE_MEMORY_TYPES),
        "statuses": sorted(MEMORY_STATUSES),
        "active_statuses": sorted(ACTIVE_STATUSES),
        "active_memory_context_limit": ACTIVE_MEMORY_CONTEXT_LIMIT,
        "task_learning_log_limit": TASK_LEARNING_LOG_LIMIT,
        "type_priority": TYPE_PRIORITY,
    }
