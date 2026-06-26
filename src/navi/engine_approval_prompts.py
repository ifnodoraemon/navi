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
    del facts, source
    # Workflow approval state is already returned as structured facts
    # (`status=awaiting_approval`, `workflow_id`, `confirmation_required`).
    # The responder/model decides how to explain that state for the current
    # user and surface; the core must not inject fixed CLI command text.
    return ""


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
    approve_command = _first_command(commands, "approve")
    reject_command = _first_command(commands, "reject")
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


def _first_command(commands: dict[str, Any], key: str) -> str:
    raw = commands.get(key)
    if isinstance(raw, list) and raw:
        return str(raw[0])
    return ""
