from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .runs import Approval, Run, RunStore
from .trust import TrustDecision, TrustStore

if TYPE_CHECKING:
    from .provider import ModelPool


class GovernanceEngine:
    """Single policy boundary for task autonomy and approvals."""

    def __init__(self, home: Path):
        self.home = home
        self.runs = RunStore(home)
        self.trust = TrustStore(home)

    async def decide_task(self, *, prompt: str, sender_id: str, workspace: str, provider: ModelPool | None = None) -> TrustDecision:
        return await self.trust.decide(prompt=prompt, sender_id=sender_id, workspace=workspace, provider=provider)

    def execution_allowed(self, task: Run) -> bool:
        if self.runs.has_approved_execution(task.id):
            return True
        if task.autonomy_level != "L3" or not task.trust_rule_id:
            return False
        rule = self.trust.get(task.trust_rule_id)
        return bool(
            rule
            and rule.autonomy_level == "L3"
            and rule.project_path
            and rule.project_path == task.workspace
        )

    def resolve_code(self, *, code: str, sender_id: str, status: str) -> Approval | None:
        return self.runs.resolve_approval(code, sender_id, status)

    def resolve_task(self, *, run_id: str, sender_id: str, status: str) -> Approval | None:
        return self.runs.resolve_run_approval(run_id, sender_id=sender_id, status=status)
