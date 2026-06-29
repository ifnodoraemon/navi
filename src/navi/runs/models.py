"""Run domain dataclasses and helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Run:
    id: str
    title: str
    status: str
    created_at: float
    updated_at: float
    kind: str = "manual"
    prompt: str = ""
    source: str = "local"
    peer_id: str = ""
    sender_id: str = ""
    provider: str = ""
    workspace: str = ""
    autonomy_level: str = "L2"
    trust_rule_id: str = ""
    why_now: str = ""
    plan_summary: str = ""
    result_summary: str = ""
    error: str = ""


@dataclass(frozen=True)
class Approval:
    id: str
    run_id: str
    code: str
    action: str
    peer_id: str
    sender_id: str
    status: str
    expires_at: float
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class Watch:
    id: str
    cron: str
    prompt: str
    peer_id: str
    sender_id: str
    enabled: bool
    next_run_at: float
    last_run_at: float
    created_at: float
    updated_at: float
    workspace: str = ""
    kind: str = "recurring"


@dataclass(frozen=True)
class ExecutionLog:
    id: str
    run_id: str
    provider: str
    phase: str
    command: str
    stdout: str
    stderr: str
    exit_code: int
    started_at: float
    ended_at: float


@dataclass(frozen=True)
class ToolCallLog:
    id: str
    tool: str
    args_json: str
    ok: bool
    facts_json: str
    error: str
    started_at: float
    ended_at: float
    run_id: str = ""
    trace_id: str = ""


def _require_workspace(workspace: str) -> str:
    value = workspace.strip()
    if not value:
        raise ValueError("workspace is required")
    return value


def _approval_resolution_facts(approval: Approval, *, now: float, sender_id: str = "") -> dict:
    return {
        "approval_id": approval.id,
        "run_id": approval.run_id,
        "code_present": bool(approval.code),
        "action": approval.action,
        "status": approval.status,
        "sender_matches": not sender_id or approval.sender_id == sender_id,
        "is_expired": approval.expires_at < now,
        "expires_at": approval.expires_at,
        "updated_at": approval.updated_at,
    }
