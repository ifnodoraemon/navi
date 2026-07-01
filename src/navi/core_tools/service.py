"""Delegation status tool handler."""
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
from typing import Any
from ..runs import RunStore
from ..tools import ToolResult


def _run_status(home: Path, args: dict[str, Any]) -> ToolResult:
    run_id = args.get("run_id")
    store = RunStore(home)
    run = store.get(str(run_id)) if run_id else None
    if run is None and not run_id:
        runs = store.list(limit=1)
        run = runs[0] if runs else None
    if run is None:
        return ToolResult(
            tool="delegate.status",
            ok=False,
            facts={"run": None, "logs": []},
            error="delegation run not found",
        )
    logs = store.list_execution_logs(run.id, limit=20)
    return ToolResult(
        tool="delegate.status",
        ok=True,
        facts={
            "run": asdict(run),
            "logs": [asdict(log) for log in logs],
        },
    )
