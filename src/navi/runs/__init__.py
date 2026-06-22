"""Navi runs package: run/approval/watch/execution-log persistence.

Re-exports the public API previously available from the monolithic
``navi/runs.py`` module. Internal structure:

- :mod:`navi.runs.models` — Run, Approval, Watch, ExecutionLog, ToolCallLog
- :mod:`navi.runs.store`  — RunStore + schema Tables
"""

from __future__ import annotations

from .models import (
    Approval,
    ExecutionLog,
    Run,
    ToolCallLog,
    Watch,
    _approval_diagnostic_facts,
    _require_workspace,
)
from .store import (
    APPROVALS_TABLE,
    EXECUTION_LOGS_TABLE,
    RUN_STORE_SCHEMA_VERSION,
    RUNS_TABLE,
    RunStore,
    TOOL_CALL_LOGS_TABLE,
    WATCHES_TABLE,
)

__all__ = [
    "APPROVALS_TABLE",
    "EXECUTION_LOGS_TABLE",
    "RUN_STORE_SCHEMA_VERSION",
    "RUNS_TABLE",
    "RunStore",
    "TOOL_CALL_LOGS_TABLE",
    "WATCHES_TABLE",
    "Approval",
    "ExecutionLog",
    "Run",
    "ToolCallLog",
    "Watch",
]
