"""Workflow persistence schema definitions."""

from __future__ import annotations

from ..schema import Column, Table

WORKFLOWS_TABLE = Table(
    "workflows",
    [
        Column("id", "TEXT", primary_key=True),
        Column("objective", "TEXT", nullable=False),
        Column("status", "TEXT", nullable=False),
        Column("source", "TEXT", nullable=False),
        Column("peer_id", "TEXT", nullable=False),
        Column("sender_id", "TEXT", nullable=False),
        Column("workspace", "TEXT", nullable=False),
        Column("permission_ceiling", "TEXT", nullable=False),
        Column("max_concurrency", "INTEGER", nullable=False),
        Column("total_subagent_limit", "INTEGER", nullable=False),
        Column("risk_class", "TEXT", nullable=False),
        Column("estimated_cost", "TEXT", nullable=False),
        Column("stop_condition", "TEXT", nullable=False),
        Column("verification_strategy", "TEXT", nullable=False),
        Column("plan_json", "TEXT", nullable=False),
        Column("evidence_json", "TEXT", nullable=False),
        Column("blocked_reason", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
        Column("updated_at", "REAL", nullable=False),
        Column("completed_at", "REAL", nullable=False),
    ],
)
WORKFLOW_STEPS_TABLE = Table(
    "workflow_steps",
    [
        Column("id", "TEXT", primary_key=True),
        Column("workflow_id", "TEXT", nullable=False),
        Column("seq", "INTEGER", nullable=False),
        Column("role", "TEXT", nullable=False),
        Column("objective", "TEXT", nullable=False),
        Column("status", "TEXT", nullable=False),
        Column("depends_on_json", "TEXT", nullable=False),
        Column("allowed_tools_json", "TEXT", nullable=False),
        Column("tool_calls_json", "TEXT", nullable=False),
        Column("evidence_json", "TEXT", nullable=False),
        Column("error", "TEXT", nullable=False),
        Column("started_at", "REAL", nullable=False),
        Column("updated_at", "REAL", nullable=False),
        Column("completed_at", "REAL", nullable=False),
    ],
)
WORKFLOW_EVENTS_TABLE = Table(
    "workflow_events",
    [
        Column("id", "TEXT", primary_key=True),
        Column("workflow_id", "TEXT", nullable=False),
        Column("event_type", "TEXT", nullable=False),
        Column("status", "TEXT", nullable=False),
        Column("step_id", "TEXT", nullable=False),
        Column("evidence_json", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
    ],
)

_SELECT_WORKFLOW = """
    SELECT id, objective, status, source, peer_id, sender_id, workspace,
           permission_ceiling, max_concurrency, total_subagent_limit,
           risk_class, estimated_cost, stop_condition, verification_strategy,
           plan_json, evidence_json, blocked_reason, created_at, updated_at, completed_at
    FROM workflows
"""
_SELECT_STEP = """
    SELECT id, workflow_id, seq, role, objective, status, depends_on_json,
           allowed_tools_json, tool_calls_json, evidence_json, error,
           started_at, updated_at, completed_at
    FROM workflow_steps
"""
_SELECT_EVENT = """
    SELECT id, workflow_id, event_type, status, step_id, evidence_json, created_at
    FROM workflow_events
"""
