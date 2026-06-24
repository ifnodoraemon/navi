"""Delegation status tool handler."""
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
from typing import Any
from ..runs import Approval, RunStore
from ..tools import ToolResult


def _approval_facts(approval: Approval) -> dict[str, Any]:
    return {
        "id": approval.id,
        "run_id": approval.run_id,
        "action": approval.action,
        "peer_id": approval.peer_id,
        "sender_id": approval.sender_id,
        "status": approval.status,
        "expires_at": approval.expires_at,
        "created_at": approval.created_at,
        "updated_at": approval.updated_at,
        "code_present": bool(approval.code),
    }


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
            facts={"run": None, "approvals": [], "logs": []},
            error="delegation run not found",
        )
    approvals = [a for a in store.list_approvals(limit=100) if a.run_id == run.id]
    logs = store.list_execution_logs(run.id, limit=20)
    return ToolResult(
        tool="delegate.status",
        ok=True,
        facts={
            "run": asdict(run),
            "approvals": [_approval_facts(approval) for approval in approvals],
            "logs": [asdict(log) for log in logs],
        },
    )
