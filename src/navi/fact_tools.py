from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .tasks import Approval, ExecutionLog, Task, TaskStore


@dataclass(frozen=True)
class ServiceFacts:
    name: str
    properties: dict[str, str]
    exit_code: int
    stderr: str


@dataclass(frozen=True)
class TaskFacts:
    task: Task | None
    approvals: list[Approval]
    logs: list[ExecutionLog]


def service_facts(name: str = "navi.service") -> ServiceFacts:
    command = [
        "systemctl",
        "--user",
        "show",
        name,
        "--property=ActiveEnterTimestamp",
        "--property=ActiveState",
        "--property=SubState",
        "--property=MainPID",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=8)
    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            properties[key] = value
    return ServiceFacts(name=name, properties=properties, exit_code=result.returncode, stderr=result.stderr.strip())


def render_service_facts(facts: ServiceFacts) -> str:
    lines = [f"Service `{facts.name}` facts:"]
    for key in ("ActiveState", "SubState", "MainPID", "ActiveEnterTimestamp"):
        lines.append(f"- {key}: {facts.properties.get(key, '-')}")
    lines.append(f"- exit_code: {facts.exit_code}")
    if facts.stderr:
        lines.append(f"- stderr: {facts.stderr}")
    return "\n".join(lines)


def task_facts(home: Path, task_id: str | None = None) -> TaskFacts:
    store = TaskStore(home)
    task = store.get(task_id) if task_id else (store.list(limit=1)[0] if store.list(limit=1) else None)
    if task is None:
        return TaskFacts(task=None, approvals=[], logs=[])
    approvals = [approval for approval in store.list_approvals(limit=100) if approval.task_id == task.id]
    logs = store.list_execution_logs(task.id, limit=20)
    return TaskFacts(task=task, approvals=approvals, logs=logs)


def render_task_facts(facts: TaskFacts) -> str:
    if facts.task is None:
        return "Task facts:\n- task: not found"
    task = facts.task
    lines = [
        f"Task `{task.id}` facts:",
        f"- status: {task.status}",
        f"- source: {task.source}",
        f"- kind: {task.kind}",
        f"- provider: {task.provider}",
        f"- workspace: {task.workspace or '-'}",
        f"- title: {task.title}",
        f"- prompt: {task.prompt}",
        f"- autonomy_level: {task.autonomy_level}",
        f"- preparation_summary: {task.plan_summary or '-'}",
        f"- result_summary: {task.result_summary or '-'}",
        f"- error: {task.error or '-'}",
    ]
    if facts.approvals:
        lines.append("- approvals:")
        for approval in facts.approvals:
            lines.append(
                f"  - code={approval.code} status={approval.status} action={approval.action} "
                f"expires_at={approval.expires_at:.0f}"
            )
    else:
        lines.append("- approvals: none")
    if facts.logs:
        lines.append("- execution_logs:")
        for log in facts.logs:
            lines.append(
                f"  - phase={log.phase} exit_code={log.exit_code} command={log.command} "
                f"stdout={_one_line(log.stdout)} stderr={_one_line(log.stderr)}"
            )
    else:
        lines.append("- execution_logs: none")
    return "\n".join(lines)


def _one_line(value: str, *, limit: int = 240) -> str:
    text = " ".join((value or "").split())
    return text[:limit] if text else "-"
