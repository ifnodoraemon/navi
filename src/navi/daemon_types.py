"""Shared types for proactive event detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .dynamic_parameters import SYSTEM_DYNAMIC_PARAMETERS

DEFAULT_PORT_PROBE_TIMEOUT_SECONDS = float(SYSTEM_DYNAMIC_PARAMETERS.get("default_port_probe_timeout_seconds", 1.0))
MAX_PROJECT_EVENT_CONCURRENCY = int(SYSTEM_DYNAMIC_PARAMETERS.get("daemon_project_event_concurrency", 4.0))
MAX_GIT_STATUS_PROMPT_CHARS = int(SYSTEM_DYNAMIC_PARAMETERS.get("max_git_status_prompt_chars", 5000.0))
MAX_LOG_READ_BYTES = int(SYSTEM_DYNAMIC_PARAMETERS.get("max_log_read_bytes", 512_000.0))
MAX_LOG_PROMPT_CHARS = int(SYSTEM_DYNAMIC_PARAMETERS.get("max_log_prompt_chars", 100_000.0))


@dataclass(frozen=True)
class ProactiveEvent:
    facts: dict[str, Any]
    state_updates: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectEventContext:
    project_path: str
    project_data: dict[str, Any]
    has_active_task: bool


EventBatch = tuple[list[ProactiveEvent], dict[str, Any]]
EventDetector = Callable[[ProjectEventContext], Awaitable[EventBatch]]
