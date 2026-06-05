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

    async def decide_task(self, *, prompt: str, sender_id: str, workspace: str, provider: ModelPool | None = None) -> TrustDecision:
        decision = await self.trust.decide(prompt=prompt, sender_id=sender_id, workspace=workspace, provider=provider)
        logger.info(f"Trust decision for sender {sender_id} in workspace {workspace}: {decision.level}")
        return decision

    def execution_allowed(self, task: Run) -> bool:
        if self.runs.has_approved_execution(task.id):
            logger.info(f"Execution allowed for task {task.id}: explicit approval found")
            return True
        if task.autonomy_level != "L3" or not task.trust_rule_id:
            logger.info(f"Execution blocked for task {task.id}: autonomy level {task.autonomy_level} < L3 or no trust rule")
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
        else:
            logger.info(f"Execution blocked for task {task.id}: not covered by L3 trust rule")
        return allowed

    def resolve_code(self, *, code: str, sender_id: str, status: str) -> Approval | None:
        approval = self.runs.resolve_approval(code, sender_id, status)
        if approval:
            logger.info(f"Resolved code {code} to status {status} by sender {sender_id}")
        return approval

    def resolve_task(self, *, run_id: str, sender_id: str, status: str) -> Approval | None:
        approval = self.runs.resolve_run_approval(run_id, sender_id=sender_id, status=status)
        if approval:
            logger.info(f"Resolved task {run_id} to status {status} by sender {sender_id}")
        return approval
