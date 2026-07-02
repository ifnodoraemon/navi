from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .approval_contract import APPROVAL_ACTION_RUN_EXECUTION
from .runs import Run, RunStore


logger = logging.getLogger("navi.governance")


class GovernanceEngine:
    """Single policy boundary for task autonomy."""

    def __init__(self, home: Path):
        self.home = home
        self.runs = RunStore(home)

    def execution_allowed(self, task: Run) -> bool:
        approval = self.runs.approved_approval_for_run(
            task.id,
            action=APPROVAL_ACTION_RUN_EXECUTION,
        )
        if approval is not None:
            return self._record_execution_grant(
                task,
                allowed=True,
                reason="approved_run_execution",
                facts={"approval_id": approval.id, "approved_by": approval.resolved_by},
            )
        if task.autonomy_level in {"L3", "L4"}:
            return self._record_execution_grant(
                task,
                allowed=True,
                reason="explicit_autonomy_level",
                facts={"autonomy_level": task.autonomy_level},
            )
        allowed, reason, facts = self._prepare_protocol_allows_execution(task)
        return self._record_execution_grant(task, allowed=allowed, reason=reason, facts=facts)

    def _prepare_protocol_allows_execution(self, task: Run) -> tuple[bool, str, dict[str, Any]]:
        protocol = self._latest_prepare_protocol(task)
        if protocol is None:
            return False, "prepare_protocol_missing", {}
        actions = _protocol_actions(protocol)
        if not actions:
            return False, "prepare_protocol_no_actions", {}

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
                return False, "prepare_protocol_tool_unavailable", {"tool": tool_name}
            safeguard = classify_capability(spec)
            requested_permission = str(action.get("permission") or spec.permission)
            if (
                requested_permission == "write"
                or safeguard.confirmation_required
                or safeguard.risk_class == "high"
            ):
                return (
                    False,
                    "explicit_approval_required",
                    {
                        "tool": tool_name,
                        "requested_permission": requested_permission,
                        "capability_risk_class": safeguard.risk_class,
                        "confirmation_required": safeguard.confirmation_required,
                    },
                )
        return True, "prepare_protocol_non_confirmation_actions", {"action_count": len(actions)}

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

    def _record_execution_grant(
        self,
        task: Run,
        *,
        allowed: bool,
        reason: str,
        facts: dict[str, Any] | None = None,
    ) -> bool:
        from .evolution import EvolutionLedger

        evidence = dict(facts or {})
        if not allowed:
            evidence["run_status"] = task.status
        before = json.dumps(
            {"allowed": False, "reason": reason, "facts": evidence},
            ensure_ascii=False,
            sort_keys=True,
        )
        after = json.dumps(
            {"allowed": allowed, "reason": reason, "facts": evidence},
            ensure_ascii=False,
            sort_keys=True,
        )
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
            before=before,
            after=after,
        )
        return allowed


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
