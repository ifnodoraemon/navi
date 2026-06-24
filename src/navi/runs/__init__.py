"""Navi runs package: run/approval/watch/execution-log persistence.

Re-exports the public API previously available from the monolithic
``navi/runs.py`` module. Internal structure:

- :mod:`navi.runs.models` — Run, Approval, Watch, ExecutionLog, ToolCallLog
- :mod:`navi.runs.store`  — RunStore + schema Tables
"""

from __future__ import annotations

from ._approval_store import APPROVALS_TABLE, ApprovalStoreMixin
from ._execution_log_store import (
    EXECUTION_LOGS_TABLE,
    TOOL_CALL_LOGS_TABLE,
    ExecutionLogStoreMixin,
)
from ._watch_store import WATCHES_TABLE, WatchStoreMixin
from .models import (
    Approval,
    ExecutionLog,
    Run,
    ToolCallLog,
    Watch,
    _approval_diagnostic_facts,
    _require_workspace,
)
from .store import RUN_STORE_SCHEMA_VERSION, RUNS_TABLE, RunStore

__all__ = [
    "APPROVALS_TABLE",
    "ApprovalStoreMixin",
    "EXECUTION_LOGS_TABLE",
    "ExecutionLogStoreMixin",
    "RUN_STORE_SCHEMA_VERSION",
    "RUNS_TABLE",
    "RunStore",
    "TOOL_CALL_LOGS_TABLE",
    "WATCHES_TABLE",
    "WatchStoreMixin",
    "Approval",
    "ExecutionLog",
    "Run",
    "ToolCallLog",
    "Watch",
    "_approval_diagnostic_facts",
    "_require_workspace",
]
