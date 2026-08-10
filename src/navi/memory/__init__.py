"""Navi governed memory package.

Internal structure:

- :mod:`navi.memory.models` — dataclasses + policy facts
- :mod:`navi.memory.provider` — MemoryProvider protocol + SQLiteMemoryProvider
- :mod:`navi.memory.store` — MemoryStore (governed CRUD + recall + learning)
"""

from __future__ import annotations

from .models import (
    ACTIVE_MEMORY_CONTEXT_LIMIT,
    ACTIVE_STATUSES,
    LEARNABLE_MEMORY_TYPES,
    MEMORY_STATUSES,
    MEMORY_TYPES,
    TASK_LEARNING_LOG_LIMIT,
    TYPE_PRIORITY,
    MemoryConflict,
    MemoryItem,
    MemoryRecall,
    SessionAlias,
    StoredMessage,
    memory_policy_facts,
)
from .provider import MemoryProvider, SQLiteMemoryProvider
from .store import MemoryStore

__all__ = [
    "ACTIVE_MEMORY_CONTEXT_LIMIT",
    "ACTIVE_STATUSES",
    "LEARNABLE_MEMORY_TYPES",
    "MEMORY_STATUSES",
    "MEMORY_TYPES",
    "TASK_LEARNING_LOG_LIMIT",
    "TYPE_PRIORITY",
    "MemoryConflict",
    "MemoryItem",
    "MemoryProvider",
    "MemoryRecall",
    "MemoryStore",
    "SQLiteMemoryProvider",
    "SessionAlias",
    "StoredMessage",
    "memory_policy_facts",
]
