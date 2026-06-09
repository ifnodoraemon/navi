from __future__ import annotations

import logging
from pathlib import Path

from .runs import Approval, Run, RunStore


logger = logging.getLogger("navi.governance")


class GovernanceEngine:
    """Single policy boundary for task autonomy and approvals."""

    def __init__(self, home: Path):
        self.home = home
        self.runs = RunStore(home)

    def execution_allowed(self, task: Run) -> bool:
        from .evolution import EvolutionLedger
        import json
        from pathlib import Path
        
        ledger = EvolutionLedger(self.home)
        
        if self.runs.has_approved_execution(task.id):
            logger.info(f"Execution allowed for task {task.id}: explicit approval found")
            ledger.record(
                run_id=task.id,
                target_type="execution_grant",
                target_id=task.id,
                reason="Explicit approval found",
                before="denied",
                after="allowed",
            )
            return True
            
        logs = self.runs.list_execution_logs(task.id)
        prepare_protocol_log = next((log for log in logs if log.phase == "prepare_protocol"), None)
        high_risk = False
        
        if prepare_protocol_log:
            try:
                from .capabilities import build_capability_registry
                from .safeguards import classify_capability
                
                registry = build_capability_registry(self.home, project_dir=Path(task.workspace))
                protocol = json.loads(prepare_protocol_log.stdout)
                
                steps = protocol.get("steps") or []
                for step in steps:
                    for action in (step.get("actions") or []):
                        tool_name = action.get("tool")
                        if tool_name:
                            spec = registry.get(tool_name)
                            if spec:
                                sg = classify_capability(spec)
                                if sg.risk_class == "high":
                                    high_risk = True
                                    break
                    if high_risk:
                        break
            except Exception as e:
                logger.error(f"Failed to analyze risk for task {task.id}: {e}")
                high_risk = True
        else:
            high_risk = True

        if high_risk:
            logger.info(
                f"Execution blocked for task {task.id}: high risk task requires explicit approval"
            )
            ledger.record(
                run_id=task.id,
                target_type="execution_grant",
                target_id=task.id,
                reason="Blocked: high risk task requires explicit approval",
                before="denied",
                after="denied",
            )
            return False
        else:
            logger.info(f"Execution allowed for task {task.id}: low risk task")
            ledger.record(
                run_id=task.id,
                target_type="execution_grant",
                target_id=task.id,
                reason="Allowed: low risk task",
                before="denied",
                after="allowed",
            )
            return True

    def resolve_code(self, *, code: str, sender_id: str, status: str) -> Approval | None:
        from .evolution import EvolutionLedger
        approval = self.runs.resolve_approval(code, sender_id, status)
        if approval:
            logger.info(f"Resolved code {code} to status {status} by sender {sender_id}")
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
            logger.info(f"Resolved task {run_id} to status {status} by sender {sender_id}")
            EvolutionLedger(self.home).record(
                run_id=run_id,
                target_type="approval",
                target_id=approval.id,
                reason=f"Task resolved to {status} by sender {sender_id}",
                before="pending",
                after=status,
            )
        return approval
