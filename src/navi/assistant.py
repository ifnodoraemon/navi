from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .cron import next_cron_time, validate_cron
from .evolution import EvolutionEngine
from .execution import ExecutionService
from .graph import GraphStore
from .tasks import Task, TaskStore
from .trust import LEVEL_LABELS, TrustDecision, TrustStore


@dataclass(frozen=True)
class AssistantCommandResult:
    text: str
    task_id: str = ""


class ActiveAssistant:
    def __init__(self, home: Path):
        self.home = home
        self.tasks = TaskStore(home)
        self.trust = TrustStore(home)
        self.graph = GraphStore(home)
        self.execution = ExecutionService(home)
        self.evolution = EvolutionEngine(home)

    async def handle_weixin_command(self, text: str, *, peer_id: str, sender_id: str) -> AssistantCommandResult:
        stripped = text.strip()
        if not stripped.startswith("/"):
            return AssistantCommandResult("")
        command, _, rest = stripped.partition(" ")
        rest = rest.strip()
        if command == "/task":
            return await self.create_task(rest, peer_id=peer_id, sender_id=sender_id, source="weixin")
        if command == "/approve":
            return self.approve(rest, sender_id=sender_id)
        if command == "/reject":
            return self.reject(rest, sender_id=sender_id)
        if command == "/status":
            return self.status(rest)
        if command == "/jobs":
            return self.jobs()
        if command == "/watch":
            return self.create_watch(rest, peer_id=peer_id, sender_id=sender_id)
        return AssistantCommandResult(
            "Unknown Navi command. Supported: /task, /approve, /reject, /status, /jobs, /watch"
        )

    async def create_task(self, prompt: str, *, peer_id: str = "", sender_id: str = "", source: str = "local") -> AssistantCommandResult:
        if not prompt:
            return AssistantCommandResult("Usage: /task <description>")
        workspace = str(Path.home())
        decision = self.trust.decide(prompt=prompt, sender_id=sender_id, workspace=workspace)
        why_now = self.why_now(
            trigger="user request",
            observation=f"{sender_id or 'local'} requested: {prompt}",
            reason=decision.why,
            action="Codex will plan first; execution depends on trust level.",
            decision=decision,
        )
        task = self.tasks.create(
            title=prompt[:120],
            prompt=prompt,
            kind="task",
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            workspace=workspace,
            autonomy_level=decision.level,
            trust_rule_id=decision.rule_id,
            why_now=why_now,
        )
        self.graph.upsert("Person", sender_id or "local", {"last_task_id": task.id, "source": source})
        self.graph.upsert("Task", task.id, {"title": task.title, "status": task.status, "prompt": task.prompt})
        planned = await self.execution.plan_task(task)
        if decision.action == "auto_execute" and decision.trusted_project:
            self.tasks.update_task(planned.id, status="queued")
            return AssistantCommandResult(
                f"{why_now}\n\nTask `{planned.id}` matched trusted autonomy and is queued for automatic execution.",
                task_id=planned.id,
            )
        approval = self.tasks.create_approval(task_id=planned.id, peer_id=peer_id, sender_id=sender_id)
        self.tasks.update_task(planned.id, status="awaiting_approval")
        return AssistantCommandResult(
            (
                f"{why_now}\n\n"
                f"Task `{planned.id}` planned by Codex.\n"
                f"Plan:\n{planned.plan_summary or '(no plan output)'}\n\n"
                f"Approve within 15 minutes with `/approve {approval.code}` or reject with `/reject {approval.code}`."
            ),
            task_id=planned.id,
        )

    def approve(self, text: str, *, sender_id: str) -> AssistantCommandResult:
        code = text.split()[0] if text else ""
        if not code:
            return AssistantCommandResult("Usage: /approve <code>")
        approval = self.tasks.resolve_approval(code, sender_id, "approved")
        if approval is None:
            return AssistantCommandResult("Approval not found for this sender.")
        if approval.status == "expired":
            return AssistantCommandResult("Approval code expired. Create a new task.")
        task = self.tasks.update_task(approval.task_id, status="queued")
        if task is None:
            return AssistantCommandResult("Approval resolved, but task was not found.")
        return AssistantCommandResult(f"Task `{task.id}` approved and queued for Codex execution.", task_id=task.id)

    def reject(self, text: str, *, sender_id: str) -> AssistantCommandResult:
        code = text.split()[0] if text else ""
        if not code:
            return AssistantCommandResult("Usage: /reject <code>")
        approval = self.tasks.resolve_approval(code, sender_id, "rejected")
        if approval is None:
            return AssistantCommandResult("Approval not found for this sender.")
        task = self.tasks.update_task(approval.task_id, status="rejected")
        if task:
            self.trust.record_failure(task)
        return AssistantCommandResult(f"Rejected task `{approval.task_id}`.")

    def status(self, text: str = "") -> AssistantCommandResult:
        task_id = text.strip()
        task = self.tasks.get(task_id) if task_id else (self.tasks.list(limit=1)[0] if self.tasks.list(limit=1) else None)
        if task is None:
            return AssistantCommandResult("No tasks yet.")
        return AssistantCommandResult(
            (
                f"Task `{task.id}`\n"
                f"Status: {task.status}\n"
                f"Autonomy: {task.autonomy_level} {LEVEL_LABELS.get(task.autonomy_level, '')}\n"
                f"Title: {task.title}\n"
                f"Plan: {task.plan_summary or '-'}\n"
                f"Result: {task.result_summary or '-'}\n"
                f"Error: {task.error or '-'}"
            ),
            task_id=task.id,
        )

    def jobs(self) -> AssistantCommandResult:
        running = [task for task in self.tasks.list(limit=20) if task.status in {"planning", "running", "queued", "awaiting_approval"}]
        watches = self.tasks.list_watches(limit=20)
        task_lines = [f"- `{task.id}` {task.status}: {task.title}" for task in running] or ["- no active tasks"]
        watch_lines = [f"- `{watch.id}` {watch.cron}: {watch.prompt}" for watch in watches] or ["- no watches"]
        return AssistantCommandResult("Active tasks:\n" + "\n".join(task_lines) + "\n\nWatches:\n" + "\n".join(watch_lines))

    def create_watch(self, text: str, *, peer_id: str, sender_id: str) -> AssistantCommandResult:
        parts = text.split(maxsplit=5)
        if len(parts) < 6:
            return AssistantCommandResult("Usage: /watch <minute> <hour> <day> <month> <weekday> <description>")
        cron = " ".join(parts[:5])
        prompt = parts[5]
        return self.create_watch_cron(cron, prompt, peer_id=peer_id, sender_id=sender_id)

    def create_watch_cron(self, cron: str, prompt: str, *, peer_id: str, sender_id: str) -> AssistantCommandResult:
        try:
            validate_cron(cron)
            next_run = next_cron_time(cron)
        except ValueError as exc:
            return AssistantCommandResult(f"Invalid cron: {exc}")
        watch = self.tasks.create_watch(cron=cron, prompt=prompt, peer_id=peer_id, sender_id=sender_id, next_run_at=next_run)
        self.graph.upsert("Watch", watch.id, {"cron": cron, "prompt": prompt, "sender_id": sender_id})
        return AssistantCommandResult(f"Watch `{watch.id}` created. Next run at {time.ctime(watch.next_run_at)}.")

    async def process_queue_once(self) -> list[Task]:
        completed = await self.execution.process_pending_once()
        reflected: list[Task] = []
        for task in completed:
            self.evolution.reflect_task(task, success=task.status == "completed")
            reflected.append(task)
        return reflected

    async def process_watches_once(self) -> list[AssistantCommandResult]:
        now = time.time()
        created: list[AssistantCommandResult] = []
        for watch in self.tasks.due_watches(now):
            result = await self.create_task(
                watch.prompt,
                peer_id=watch.peer_id,
                sender_id=watch.sender_id,
                source="watch",
            )
            created.append(result)
            self.tasks.mark_watch_run(watch.id, last_run_at=now, next_run_at=next_cron_time(watch.cron, now=now))
        return created

    @staticmethod
    def why_now(
        *,
        trigger: str,
        observation: str,
        reason: str,
        action: str,
        decision: TrustDecision,
    ) -> str:
        return (
            "Why now:\n"
            f"- Trigger: {trigger}\n"
            f"- Observation: {observation}\n"
            f"- Reason: {reason}\n"
            f"- Suggested action: {action}\n"
            f"- Autonomy: {decision.level} {LEVEL_LABELS.get(decision.level, '')}"
        )
