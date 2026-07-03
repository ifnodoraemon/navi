"""Shared types for proactive event detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

DEFAULT_DEV_PORTS = [3000, 5000, 8000, 8080]
DEFAULT_PORT_PROBE_TIMEOUT_SECONDS = 1.0
MAX_PROJECT_EVENT_CONCURRENCY = 4
MAX_GIT_STATUS_PROMPT_CHARS = 5000
LOG_ERROR_KEYWORDS = ("exception", "fatal", "traceback (most recent call last):")
MAX_LOG_READ_BYTES = 512_000
MAX_LOG_PROMPT_CHARS = 100_000
MAX_FAILED_WATCH_RUN_RECORDS = 50


@dataclass(frozen=True)
class ProactiveEvent:
    source: str
    message: str
    facts: dict[str, Any]
    state_updates: dict[str, Any] = field(default_factory=dict)
    suppressed_state_updates: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectEventContext:
    project_path: str
    project_data: dict[str, Any]
    has_active_task: bool
    use_default_ports: bool


EventBatch = tuple[list[ProactiveEvent], dict[str, Any]]
EventDetector = Callable[[ProjectEventContext], Awaitable[EventBatch]]
