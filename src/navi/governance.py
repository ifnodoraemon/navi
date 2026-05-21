from __future__ import annotations

from pathlib import Path

from .tasks import Approval, Task, TaskStore
from .trust import TrustDecision, TrustStore


class GovernanceEngine:
    """Single policy boundary for task autonomy and approvals."""

    def __init__(self, home: Path):
        self.home = home
        self.tasks = TaskStore(home)
        self.trust = TrustStore(home)

    def decide_task(self, *, prompt: str, sender_id: str, workspace: str) -> TrustDecision:
        return self.trust.decide(prompt=prompt, sender_id=sender_id, workspace=workspace)

    def execution_allowed(self, task: Task) -> bool:
        if self.tasks.has_approved_execution(task.id):
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
        return self.tasks.resolve_approval(code, sender_id, status)

    def resolve_task(self, *, task_id: str, sender_id: str, status: str) -> Approval | None:
        return self.tasks.resolve_task_approval(task_id, sender_id=sender_id, status=status)
