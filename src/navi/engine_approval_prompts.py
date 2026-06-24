"""Approval prompt rendering for HernessEngine."""

from __future__ import annotations

import time
from typing import Any, Callable

from .connector_registry import approval_surface_affordance

ApprovalPromptRenderer = Callable[[dict[str, Any], str], str]


def _render_approval_prompt(facts: dict[str, Any] | None, *, source: str = "") -> str:
    if not facts or facts.get("status") != "awaiting_approval":
        return ""
    for renderer in APPROVAL_PROMPT_RENDERERS:
        rendered = renderer(facts, source)
        if rendered:
            return rendered
    return ""


def _workflow_approval_prompt(facts: dict[str, Any], source: str) -> str:
    del source
    workflow_id = str(facts.get("workflow_id") or "").strip()
    if not workflow_id:
        return ""
    step_count = facts.get("step_count")
    risk_class = str(facts.get("risk_class") or "unknown")
    estimated_cost = str(facts.get("estimated_cost") or "unknown")
    stop_condition = str(facts.get("stop_condition") or "").strip()
    details = [
        f"Workflow ID: `{workflow_id}`",
        f"Steps: {step_count}" if step_count is not None else "",
        f"Risk class: {risk_class}",
        f"Estimated cost: {estimated_cost}",
        f"Stop condition: {stop_condition}" if stop_condition else "",
    ]
    detail_text = "\n".join(f"- {item}" for item in details if item)
    return (
        "Workflow proposal is awaiting confirmation before execution.\n"
        f"{detail_text}\n"
        f"Approve: `navi workflow approve {workflow_id}`\n"
        f"Reject: `navi workflow reject {workflow_id}`"
    ).strip()


def _run_approval_prompt(facts: dict[str, Any], source: str) -> str:
    approval = facts.get("approval")
    if not isinstance(approval, dict):
        return ""
    code = str(approval.get("code") or "").strip()
    if not code:
        return ""
    run_id = str(facts.get("run_id") or "").strip()
    expires_at = approval.get("expires_at")
    try:
        minutes = max(0, round((float(expires_at) - time.time()) / 60)) if expires_at else 0
    except (TypeError, ValueError):
        minutes = 0
    expiry = f"Approval expires in ~{minutes} minutes." if minutes else "Approval is expiring soon."
    affordance = approval_surface_affordance(source)
    commands = (
        affordance.get("approval_commands")
        if isinstance(affordance.get("approval_commands"), dict)
        else {}
    )
    approve_command = _first_command(commands, "approve", "approve")
    reject_command = _first_command(commands, "reject", "reject")
    template = str(affordance.get("approval_template") or "")
    if not template:
        return ""
    diff = str(approval.get("diff") or "").strip()
    diff_text = f"\n\nProposed Changes:\n```diff\n{diff}\n```" if diff else ""
    return (
        template.format(
            task_line=f"Task ID: `{run_id}`" if run_id else "",
            code=code,
            expiry=expiry,
            approve_command=approve_command,
            reject_command=reject_command,
        ).strip()
        + diff_text
    )


APPROVAL_PROMPT_RENDERERS: tuple[ApprovalPromptRenderer, ...] = (
    _workflow_approval_prompt,
    _run_approval_prompt,
)


def _first_command(commands: dict[str, Any], key: str, fallback: str) -> str:
    raw = commands.get(key)
    if isinstance(raw, list) and raw:
        return str(raw[0])
    return fallback
