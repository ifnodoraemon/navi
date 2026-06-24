from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .runs import Approval, Run, RunStore


logger = logging.getLogger("navi.governance")


class GovernanceEngine:
    """Single policy boundary for task autonomy and approvals."""

    def __init__(self, home: Path):
        self.home = home
        self.runs = RunStore(home)

    def execution_allowed(self, task: Run) -> bool:
        if self.runs.has_approved_execution(task.id):
            return self._record_execution_grant(
                task, allowed=True, reason="Explicit approval found"
            )
        if task.autonomy_level in {"L3", "L4"}:
            return self._record_execution_grant(
                task,
                allowed=True,
                reason=f"Allowed by explicit autonomy level {task.autonomy_level}",
            )
        allowed, reason = self._prepare_protocol_allows_execution(task)
        return self._record_execution_grant(task, allowed=allowed, reason=reason)

    def _prepare_protocol_allows_execution(self, task: Run) -> tuple[bool, str]:
        protocol = self._latest_prepare_protocol(task)
        if protocol is None:
            return False, "Blocked: no prepare protocol facts found"
        actions = _protocol_actions(protocol)
        if not actions:
            return False, "Blocked: prepare protocol declared no capability actions"

        from .capabilities import build_capability_registry
        from .safeguards import classify_capability

        registry = build_capability_registry(
            self.home,
            project_dir=Path(task.workspace or self.home),
            permission_ceiling="write",
        )
        for action in actions:
            tool_name = str(action.get("tool") or "").strip()
            spec = registry.get(tool_name) if tool_name else None
            if spec is None:
                return False, f"Blocked: prepare protocol references unavailable tool {tool_name}"
            safeguard = classify_capability(spec)
            requested_permission = str(action.get("permission") or spec.permission)
            if (
                requested_permission == "write"
                or safeguard.confirmation_required
                or safeguard.risk_class == "high"
            ):
                return (
                    False,
                    f"Blocked: {tool_name} requires explicit approval ({safeguard.reason})",
                )
        return True, "Allowed: prepare protocol contains only non-confirmation capability actions"

    def _latest_prepare_protocol(self, task: Run) -> dict[str, Any] | None:
        logs = self.runs.list_execution_logs(task.id)
        for log in logs:
            if log.phase != "prepare_protocol":
                continue
            try:
                payload = json.loads(log.stdout)
            except json.JSONDecodeError:
                logger.warning("Invalid prepare protocol JSON for task %s", task.id)
                return None
            if isinstance(payload, dict):
                return (
                    payload.get("navi_execution")
                    if isinstance(payload.get("navi_execution"), dict)
                    else payload
                )
        return None

    def _record_execution_grant(self, task: Run, *, allowed: bool, reason: str) -> bool:
        from .evolution import EvolutionLedger

        # Principle 8: an approval failure must say which state is missing,
        # not pretend the agent has no local capability. Attach the approval
        # resolution diagnostic so the caller can act (re-issue code, approve,
        # etc.) instead of guessing.
        if not allowed:
            diagnostic = self.runs.approval_resolution_diagnostic(
                run_id=task.id, sender_id=task.sender_id
            )
            reason = f"{reason} | approval_state={diagnostic.get('reason', 'unknown')} run_status={task.status}"
        logger.info(
            "Execution %s for task %s: %s",
            "allowed" if allowed else "blocked",
            task.id,
            reason,
        )
        EvolutionLedger(self.home).record(
            run_id=task.id,
            target_type="execution_grant",
            target_id=task.id,
            reason=reason,
            before="denied",
            after="allowed" if allowed else "denied",
        )
        return allowed

    def resolve_code(self, *, code: str, sender_id: str, status: str) -> Approval | None:
        from .evolution import EvolutionLedger

        approval = self.runs.resolve_approval(code, sender_id, status)
        if approval:
            logger.info("Resolved code %s to status %s by sender %s", code, status, sender_id)
            EvolutionLedger(self.home).record(
                run_id=approval.run_id,
                target_type="approval",
                target_id=approval.id,
                reason=f"Resolved to {status} by sender {sender_id}",
                before="pending",
                after=status,
            )
        return approval

    def resolve_task(self, *, run_id: str, sender_id: str, status: str) -> Approval | None:
        from .evolution import EvolutionLedger

        approval = self.runs.resolve_run_approval(run_id, sender_id=sender_id, status=status)
        if approval:
            logger.info("Resolved task %s to status %s by sender %s", run_id, status, sender_id)
            EvolutionLedger(self.home).record(
                run_id=run_id,
                target_type="approval",
                target_id=approval.id,
                reason=f"Task resolved to {status} by sender {sender_id}",
                before="pending",
                after=status,
            )
        return approval


def _protocol_actions(protocol: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    steps = protocol.get("steps")
    if not isinstance(steps, list):
        return actions
    for step in steps:
        if not isinstance(step, dict):
            continue
        raw_actions = step.get("actions")
        if isinstance(raw_actions, list):
            actions.extend(action for action in raw_actions if isinstance(action, dict))
    return actions
