"""Delegation state tool handler."""
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
from typing import Any
from ..runs import RunStore
from ..tools import ToolResult


def _run_state(home: Path, args: dict[str, Any]) -> ToolResult:
    run_id = args.get("run_id")
    store = RunStore(home)
    run = store.get(str(run_id)) if run_id else None
    if run is None and not run_id:
        runs = store.list(limit=1)
        run = runs[0] if runs else None
    if run is None:
        return ToolResult(
            tool="delegate.state",
            ok=False,
            facts={"run": None, "logs": []},
            error="delegation run not found",
        )
    logs = store.list_execution_logs(run.id, limit=20)
    return ToolResult(
        tool="delegate.state",
        ok=True,
        facts={
            "run": _run_state_facts(run),
            "logs": [asdict(log) for log in logs],
        },
    )


def _run_state_facts(run: Any) -> dict[str, Any]:
    return {
        "id": run.id,
        "title": run.title,
        "phase": run.phase,
        "governance": run.governance,
        "acceptance": run.acceptance,
        "resolution": run.resolution,
        "kind": run.kind,
        "source": run.source,
        "peer_id": run.peer_id,
        "sender_id": run.sender_id,
        "provider": run.provider,
        "workspace": run.workspace,
        "autonomy_level": run.autonomy_level,
        "trust_rule_id": run.trust_rule_id,
        "why_now": run.why_now,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "error": run.error,
    }
