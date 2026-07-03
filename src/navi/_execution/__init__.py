"""Execution subsystem: protocol, provider, and service orchestration."""

from __future__ import annotations

from .protocol import (
    EXECUTION_PROTOCOL_VERSION,
    INTERNAL_EXECUTION_PROVIDER,
    SUBAGENT_EXECUTOR_ROLE,
    SUBAGENT_NOTIFICATION_ROLE,
    SUBAGENT_PLANNER_ROLE,
    ExecutionProtocol,
    ExecutionResult,
    execution_output_schema,
    execution_protocol_instruction,
    require_workspace_value,
    task_workspace,
)
from .provider import (
    NaviExecutionProvider,
    get_engine_class,
    register_engine_class,
)
from .service import ExecutionService

__all__ = [
    "EXECUTION_PROTOCOL_VERSION",
    "ExecutionProtocol",
    "ExecutionResult",
    "ExecutionService",
    "INTERNAL_EXECUTION_PROVIDER",
    "NaviExecutionProvider",
    "SUBAGENT_EXECUTOR_ROLE",
    "SUBAGENT_NOTIFICATION_ROLE",
    "SUBAGENT_PLANNER_ROLE",
    "execution_output_schema",
    "execution_protocol_instruction",
    "get_engine_class",
    "register_engine_class",
    "require_workspace_value",
    "task_workspace",
]
