from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .runs import Approval, Run, RunStore
from .trust import TrustDecision, TrustStore

if TYPE_CHECKING:
    from .provider import ModelPool


logger = logging.getLogger("navi.governance")


class GovernanceEngine:
    """Single policy boundary for task autonomy and approvals."""

    def __init__(self, home: Path):
        self.home = home
        self.runs = RunStore(home)
        self.trust = TrustStore(home)

    async def decide_task(
        self, *, prompt: str, sender_id: str, workspace: str, provider: ModelPool | None = None
    ) -> TrustDecision:
        decision = await self.trust.decide(
            prompt=prompt, sender_id=sender_id, workspace=workspace, provider=provider
        )
        logger.info(
            f"Trust decision for sender {sender_id} in workspace {workspace}: {decision.level}"
        )
        from .evolution import EvolutionLedger
        import json
        EvolutionLedger(self.home).record(
            run_id=f"trust:{sender_id}",
            target_type="trust_decision",
            target_id=sender_id,
            reason=f"Trust evaluation for prompt '{prompt[:50]}...'",
            before="unknown",
            after=json.dumps({"level": decision.level, "why": decision.why}, default=str),
        )
        return decision

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
                            spec = registry.get_spec(tool_name)
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

        required_autonomy = "L3" if high_risk else "L2"
        level_values = {"L1": 1, "L2": 2, "L3": 3}
        task_level = level_values.get(task.autonomy_level, 0)
        req_level = level_values.get(required_autonomy, 3)
        
        if task_level < req_level:
            logger.info(
                f"Execution blocked for task {task.id}: autonomy level {task.autonomy_level} < required {required_autonomy} (high_risk={high_risk})"
            )
            ledger.record(
                run_id=task.id,
                target_type="execution_grant",
                target_id=task.id,
                reason=f"Blocked: autonomy {task.autonomy_level} < required {required_autonomy}",
                before="denied",
                after="denied",
            )
            return False
            
        if required_autonomy == "L3":
            if not task.trust_rule_id:
                logger.info(f"Execution blocked for task {task.id}: L3 required but no trust rule")
                ledger.record(
                    run_id=task.id,
                    target_type="execution_grant",
                    target_id=task.id,
                    reason="Blocked: L3 required but no trust rule",
                    before="denied",
                    after="denied",
                )
                return False
                
            rule = self.trust.get(task.trust_rule_id)
            allowed = bool(
                rule
                and rule.autonomy_level == "L3"
                and rule.project_path
                and rule.project_path == task.workspace
            )
            
            if allowed:
                logger.info(f"Execution allowed for task {task.id}: covered by L3 trust rule {rule.id}")
                ledger.record(
                    run_id=task.id,
                    target_type="execution_grant",
                    target_id=task.id,
                    reason=f"Covered by L3 trust rule {rule.id}",
                    before="denied",
                    after="allowed",
                )
                return True
            else:
                logger.info(f"Execution blocked for task {task.id}: not covered by L3 trust rule")
                ledger.record(
                    run_id=task.id,
                    target_type="execution_grant",
                    target_id=task.id,
                    reason="Not covered by L3 trust rule",
                    before="denied",
                    after="denied",
                )
                return False
        else:
            logger.info(f"Execution allowed for task {task.id}: {required_autonomy} sufficient for low risk")
            ledger.record(
                run_id=task.id,
                target_type="execution_grant",
                target_id=task.id,
                reason=f"Allowed: {required_autonomy} sufficient for low risk",
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
