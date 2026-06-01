from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .defaults import DEFAULT_SERVICE_NAME
from .runs import Approval, ExecutionLog, Run, RunStore


@dataclass(frozen=True)
class ServiceFacts:
    name: str
    properties: dict[str, str]
    exit_code: int
    stderr: str


@dataclass(frozen=True)
class RunFacts:
    run: Run | None
    approvals: list[Approval]
    logs: list[ExecutionLog]


def default_service_name() -> str:
    return os.environ.get("NAVI_SERVICE_NAME", DEFAULT_SERVICE_NAME)


def service_facts(name: str | None = None) -> ServiceFacts:
    name = name or default_service_name()
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
    if result.returncode != 0:
        fallback = subprocess.run(
            ["systemctl", "--user", "is-active", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
        active_state = fallback.stdout.strip()
        if fallback.returncode == 0 and active_state:
            properties["ActiveState"] = active_state
            properties.setdefault("SubState", active_state)
            return ServiceFacts(name=name, properties=properties, exit_code=0, stderr=result.stderr.strip())
    return ServiceFacts(name=name, properties=properties, exit_code=result.returncode, stderr=result.stderr.strip())


def render_service_facts(facts: ServiceFacts) -> str:
    lines = [f"Service `{facts.name}` facts:"]
    for key in ("ActiveState", "SubState", "MainPID", "ActiveEnterTimestamp"):
        lines.append(f"- {key}: {facts.properties.get(key, '-')}")
    lines.append(f"- exit_code: {facts.exit_code}")
    if facts.stderr:
        lines.append(f"- stderr: {facts.stderr}")
    return "\n".join(lines)


def run_facts(home: Path, run_id: str | None = None) -> RunFacts:
    store = RunStore(home)
    run = store.get(run_id) if run_id else (store.list(limit=1)[0] if store.list(limit=1) else None)
    if run is None:
        return RunFacts(run=None, approvals=[], logs=[])
    approvals = [approval for approval in store.list_approvals(limit=100) if approval.run_id == run.id]
    logs = store.list_execution_logs(run.id, limit=20)
    return RunFacts(run=run, approvals=approvals, logs=logs)


def render_run_facts(facts: RunFacts) -> str:
    if facts.run is None:
        return "Run facts:\n- run: not found"
    run = facts.run
    lines = [
        f"Run `{run.id}` facts:",
        f"- status: {run.status}",
        f"- source: {run.source}",
        f"- kind: {run.kind}",
        f"- provider: {run.provider}",
        f"- workspace: {run.workspace or '-'}",
        f"- title: {run.title}",
        f"- prompt: {run.prompt}",
        f"- autonomy_level: {run.autonomy_level}",
        f"- preparation_summary: {run.plan_summary or '-'}",
        f"- result_summary: {run.result_summary or '-'}",
        f"- error: {run.error or '-'}",
    ]
    if facts.approvals:
        lines.append("- approvals:")
        for approval in facts.approvals:
            lines.append(
                f"  - status={approval.status} action={approval.action} code_present={bool(approval.code)} "
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
