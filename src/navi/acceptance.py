from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .app_factory import build_runtime
from .connector_runtime import LOCAL_CONVERSATIONAL_TOOL_POLICY
from .daemon import SystemDaemon
from .engine import AgentTurnResult, LoopEngine
from .goals import GoalStore
from .lifecycle import (
    Phase,
    Resolution,
    acceptance_advance,
    acceptance_outcome,
)
from .runs import ExecutionLog, RunStore


@dataclass(frozen=True)
class AcceptanceScenario:
    id: str
    request: str
    approval_template: str = "approve {{approval_code}}"
    source: str = "acceptance"
    peer_id: str = "acceptance"
    sender_id: str = "acceptance"
    workspace: str = ""
    expected: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AcceptanceReport:
    id: str
    accepted: bool
    outcome: str
    reason: str
    workspace: str
    run_id: str = ""
    trace_id: str = ""
    run_phase: str = ""
    run_resolution: str = ""
    goal_phase: str = ""
    goal_resolution: str = ""
    turns: list[dict[str, Any]] = field(default_factory=list)
    progress: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


AcceptanceCheckRule = Callable[[dict[str, Any]], dict[str, Any]]


def load_acceptance_scenario(path: Path) -> AcceptanceScenario:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    data = loaded or {}
    if not isinstance(data, dict):
        raise ValueError("acceptance scenario must be a mapping")
    scenario_id = str(data.get("id") or path.stem).strip()
    request = str(data.get("request") or "").strip()
    if not request:
        raise ValueError("acceptance scenario requires request")
    raw_expected = data.get("expected")
    expected = raw_expected if isinstance(raw_expected, dict) else {}
    return AcceptanceScenario(
        id=scenario_id,
        request=request,
        approval_template=str(data.get("approval_template") or "approve {{approval_code}}"),
        source=str(data.get("source") or "acceptance"),
        peer_id=str(data.get("peer_id") or "acceptance"),
        sender_id=str(data.get("sender_id") or "acceptance"),
        workspace=str(data.get("workspace") or ""),
        expected=expected,
    )


async def run_product_acceptance(
    *,
    home: Path,
    project_dir: Path | None,
    scenario: AcceptanceScenario,
    auto_approve: bool = True,
) -> AcceptanceReport:
    workspace = _acceptance_workspace(project_dir, scenario)
    workspace.mkdir(parents=True, exist_ok=True)

    runs = RunStore(home)
    known_run_ids = {run.id for run in runs.list(limit=200)}
    runtime = build_runtime(home)
    daemon = SystemDaemon(home, project_dir=workspace)
    engine = LoopEngine(
        home=home,
        runtime=runtime,
        project_dir=workspace,
        disabled_capability_classes=LOCAL_CONVERSATIONAL_TOOL_POLICY.blocked_capability_classes,
        event_bus=daemon.event_bus,
    )

    turns: list[dict[str, Any]] = []
    progress: list[dict[str, Any]] = []
    errors: list[str] = []

    initial = await engine.handle(
        scenario.request,
        peer_id=scenario.peer_id,
        sender_id=scenario.sender_id,
        source=scenario.source,
    )
    turns.append(_turn_facts("request", initial))
    run_id = _run_id_from_turn(initial) or _latest_new_run_id(runs, known_run_ids)
    if not run_id:
        return _report(
            scenario,
            workspace=workspace,
            accepted=False,
            outcome="failed",
            reason="user request did not produce a delegated run",
            turns=turns,
            progress=progress,
            errors=["no run_id from turn result or run store"],
        )

    while True:
        before = _state_snapshot(runs, run_id)
        run = runs.get(run_id)
        if run is None:
            errors.append("run disappeared")
            break
        advance = acceptance_advance(
            phase=run.phase,
            governance=run.governance,
            resolution=run.resolution,
        )
        if advance.terminal:
            break
        if advance.action == "approve":
            if not auto_approve:
                errors.append("approval required")
                break
            approval = runs.pending_approval_for_run(run_id, sender_id=scenario.sender_id)
            if approval is None:
                errors.append("run is awaiting approval but no pending approval exists")
                break
            approval_text = scenario.approval_template.replace(
                "{{approval_code}}", approval.code
            ).replace("{{run_id}}", run_id)
            approved = await engine.handle(
                approval_text,
                peer_id=scenario.peer_id,
                sender_id=scenario.sender_id,
                source=scenario.source,
                session_id=initial.session_id or None,
            )
            turns.append(_turn_facts("approval", approved))
        elif advance.action == "process_queue":
            processed = await daemon.process_queue_once()
            progress.append(
                {
                    "phase": "process_queue",
                    "processed": [item.id for item in processed],
                }
            )
        else:
            errors.append(advance.error)
            break

        after = _state_snapshot(runs, run_id)
        progress.append({"phase": "state", "before": before, "after": after})
        if after == before:
            errors.append("product state made no progress")
            break

    return _evaluate_acceptance(
        scenario,
        home=home,
        workspace=workspace,
        run_id=run_id,
        trace_id=initial.trace_id,
        turns=turns,
        progress=progress,
        errors=errors,
    )


def report_to_text(report: AcceptanceReport) -> str:
    marker = "accepted" if report.accepted else report.outcome
    lines = [
        f"product_acceptance={marker}",
        f"scenario={report.id}",
        f"reason={report.reason}",
        f"workspace={report.workspace}",
    ]
    if report.run_id:
        lines.append(
            f"run={report.run_id} phase={report.run_phase or '-'} resolution={report.run_resolution or '-'}"
        )
    if report.goal_phase:
        lines.append(
            f"goal_phase={report.goal_phase} goal_resolution={report.goal_resolution or '-'}"
        )
    if report.errors:
        lines.append("errors:")
        lines.extend(f"- {error}" for error in report.errors)
    failed_checks = [check for check in report.checks if not check.get("ok")]
    if failed_checks:
        lines.append("failed_checks:")
        lines.extend(f"- {check.get('name')}: {check.get('error')}" for check in failed_checks)
    return "\n".join(lines)


def _evaluate_acceptance(
    scenario: AcceptanceScenario,
    *,
    home: Path,
    workspace: Path,
    run_id: str,
    trace_id: str,
    turns: list[dict[str, Any]],
    progress: list[dict[str, Any]],
    errors: list[str],
) -> AcceptanceReport:
    runs = RunStore(home)
    goals = GoalStore(home)
    run = runs.get(run_id)
    goal = goals.get_by_run(run_id)
    logs = runs.list_execution_logs(run_id, limit=200)
    protocol = _latest_protocol(logs, phase="execute_protocol")
    checks = _acceptance_checks(
        scenario.expected,
        workspace=workspace,
        run=run,
        goal_phase=goal.phase if goal else "",
        goal_resolution=goal.resolution if goal else "",
        protocol=protocol,
    )
    all_checks_ok = all(bool(check.get("ok")) for check in checks)
    run_phase = run.phase if run else ""
    run_resolution = run.resolution if run else ""
    goal_phase = goal.phase if goal else ""
    goal_resolution = goal.resolution if goal else ""
    protocol_completion = _dict_path(protocol, "completion", "status")
    protocol_verification = _dict_path(protocol, "verification", "status")
    failed_evidence = _failed_evidence(protocol)

    if run is None:
        errors.append("run not found")
    if goal is None:
        errors.append("goal not found for run")
    if not protocol:
        errors.append("execute protocol log missing")
    if failed_evidence:
        errors.append("execution evidence contains failed capability or verification result")

    accepted = (
        not errors
        and run_phase == Phase.ENDED
        and run_resolution == Resolution.SUCCESS
        and goal_phase == Phase.ENDED
        and goal_resolution == Resolution.SUCCESS
        and protocol_completion == Resolution.SUCCESS
        and protocol_verification == "verified"
        and all_checks_ok
    )

    reason = "product objective completed with verified evidence"
    outcome = acceptance_outcome(accepted=accepted, run_phase=run_phase)
    if not accepted:
        reason = _acceptance_failure_reason(
            errors=errors,
            checks=checks,
            run_phase=run_phase,
            run_resolution=run_resolution,
            goal_phase=goal_phase,
            goal_resolution=goal_resolution,
            protocol_completion=protocol_completion,
            protocol_verification=protocol_verification,
        )

    return _report(
        scenario,
        workspace=workspace,
        accepted=accepted,
        outcome=outcome,
        reason=reason,
        turns=turns,
        progress=progress,
        errors=errors,
        run_id=run_id,
        trace_id=trace_id,
        run_phase=run_phase,
        run_resolution=run_resolution,
        goal_phase=goal_phase,
        goal_resolution=goal_resolution,
        checks=checks,
        evidence={
            "protocol": _compact_protocol(protocol),
            "execution_logs": [_log_facts(log) for log in logs[:12]],
        },
    )


def _report(
    scenario: AcceptanceScenario,
    *,
    workspace: Path,
    accepted: bool,
    outcome: str,
    reason: str,
    turns: list[dict[str, Any]],
    progress: list[dict[str, Any]],
    errors: list[str],
    run_id: str = "",
    trace_id: str = "",
    run_phase: str = "",
    run_resolution: str = "",
    goal_phase: str = "",
    goal_resolution: str = "",
    checks: list[dict[str, Any]] | None = None,
    evidence: dict[str, Any] | None = None,
) -> AcceptanceReport:
    return AcceptanceReport(
        id=scenario.id,
        accepted=accepted,
        outcome=outcome,
        reason=reason,
        workspace=str(workspace),
        run_id=run_id,
        trace_id=trace_id,
        run_phase=run_phase,
        run_resolution=run_resolution,
        goal_phase=goal_phase,
        goal_resolution=goal_resolution,
        turns=turns,
        progress=progress,
        checks=checks or [],
        errors=errors,
        evidence=evidence or {},
    )


def _acceptance_workspace(project_dir: Path | None, scenario: AcceptanceScenario) -> Path:
    if scenario.workspace:
        return Path(scenario.workspace).expanduser().resolve()
    if project_dir is not None:
        return project_dir.expanduser().resolve()
    root = Path(tempfile.gettempdir()) / "navi-product-acceptance"
    return (root / scenario.id).resolve()


def _turn_facts(kind: str, result: AgentTurnResult) -> dict[str, Any]:
    return {
        "kind": kind,
        "action": result.action,
        "run_id": result.run_id,
        "trace_id": result.trace_id,
        "terminal": result.terminal,
        "text": result.text[:1600],
        "facts": result.facts or {},
    }


def _run_id_from_turn(result: AgentTurnResult) -> str:
    facts = result.facts or {}
    return str(result.run_id or facts.get("run_id") or "").strip()


def _latest_new_run_id(runs: RunStore, known_run_ids: set[str]) -> str:
    for run in runs.list(limit=50):
        if run.id not in known_run_ids:
            return run.id
    return ""


def _state_snapshot(runs: RunStore, run_id: str) -> dict[str, Any]:
    run = runs.get(run_id)
    logs = runs.list_execution_logs(run_id, limit=200)
    approvals = [item for item in runs.list_approvals(limit=200) if item.run_id == run_id]
    return {
        "run_phase": run.phase if run else "",
        "run_governance": run.governance if run else "",
        "run_resolution": run.resolution if run else "",
        "approval_statuses": [item.status for item in approvals],
        "log_count": len(logs),
        "last_log_exit_code": logs[0].exit_code if logs else None,
    }


def _acceptance_checks(
    expected: dict[str, Any],
    *,
    workspace: Path,
    run: Any,
    goal_phase: str,
    goal_resolution: str,
    protocol: dict[str, Any],
) -> list[dict[str, Any]]:
    context = {
        "run": run,
        "goal_phase": goal_phase,
        "goal_resolution": goal_resolution,
        "protocol": protocol,
    }
    checks = [rule(context) for rule in ACCEPTANCE_CHECK_RULES]
    file_contains = expected.get("file_contains")
    if isinstance(file_contains, dict):
        checks.append(_file_contains_check(file_contains, workspace=workspace))
    for item in expected.get("files", []) if isinstance(expected.get("files"), list) else []:
        if isinstance(item, dict):
            checks.append(_file_contains_check(item, workspace=workspace))
    return checks


def _acceptance_run_completed(context: dict[str, Any]) -> dict[str, Any]:
    run = context.get("run")
    phase = run.phase if run else ""
    resolution = run.resolution if run else ""
    detail = f"phase={phase} resolution={resolution}"
    return _check(
        "run.completed",
        phase == Phase.ENDED and resolution == Resolution.SUCCESS,
        detail,
    )


def _acceptance_goal_verified(context: dict[str, Any]) -> dict[str, Any]:
    phase = str(context.get("goal_phase") or "")
    resolution = str(context.get("goal_resolution") or "")
    detail = f"phase={phase} resolution={resolution}"
    return _check(
        "goal.verified",
        phase == Phase.ENDED and resolution == Resolution.SUCCESS,
        detail,
    )


def _acceptance_protocol_completed(context: dict[str, Any]) -> dict[str, Any]:
    raw_protocol = context.get("protocol")
    protocol = raw_protocol if isinstance(raw_protocol, dict) else {}
    status = str(_dict_path(protocol, "completion", "status") or "")
    return _check("protocol.completed", status == Resolution.SUCCESS, status)


def _acceptance_protocol_verified(context: dict[str, Any]) -> dict[str, Any]:
    raw_protocol = context.get("protocol")
    protocol = raw_protocol if isinstance(raw_protocol, dict) else {}
    status = str(_dict_path(protocol, "verification", "status") or "")
    return _check("protocol.verified", status == "verified", status)


def _acceptance_run_summary(context: dict[str, Any]) -> dict[str, Any]:
    run = context.get("run")
    summary = str(run.result_summary if run else "").strip()
    return _check("run.summary", bool(summary), "run completed without user-facing result summary")


ACCEPTANCE_CHECK_RULES: tuple[AcceptanceCheckRule, ...] = (
    _acceptance_run_completed,
    _acceptance_goal_verified,
    _acceptance_protocol_completed,
    _acceptance_protocol_verified,
    _acceptance_run_summary,
)


def _check(name: str, ok: bool, error: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "error": "" if ok else error}


def _file_contains_check(spec: dict[str, Any], *, workspace: Path) -> dict[str, Any]:
    raw_path = str(spec.get("path") or "").strip()
    expected = str(spec.get("text") or "")
    if not raw_path:
        return _check("file.contains", False, "expected file path is empty")
    path = Path(raw_path)
    if not path.is_absolute():
        path = workspace / path
    try:
        resolved = path.resolve()
        root = workspace.resolve()
        if resolved != root and root not in resolved.parents:
            return _check("file.contains", False, "expected file is outside workspace")
        content = resolved.read_text(encoding="utf-8") if resolved.exists() else ""
    except OSError as exc:
        return _check("file.contains", False, str(exc))
    ok = bool(expected and expected in content)
    return {
        "name": "file.contains",
        "ok": ok,
        "path": str(resolved),
        "error": "" if ok else "expected text not found",
    }


def _latest_protocol(logs: list[ExecutionLog], *, phase: str) -> dict[str, Any]:
    for log in logs:
        if log.phase != phase:
            continue
        try:
            parsed = json.loads(log.stdout)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _failed_evidence(protocol: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = protocol.get("evidence") if isinstance(protocol, dict) else []
    if not isinstance(evidence, list):
        return []
    failed: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        if item.get("kind") in {"capability_result", "verification_result"} and not item.get("ok"):
            failed.append(item)
    return failed


def _dict_path(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _compact_protocol(protocol: dict[str, Any]) -> dict[str, Any]:
    if not protocol:
        return {}
    raw_evidence = protocol.get("evidence")
    evidence = raw_evidence if isinstance(raw_evidence, list) else []
    return {
        "phase": protocol.get("phase"),
        "completion": protocol.get("completion"),
        "verification": protocol.get("verification"),
        "evidence_count": len(evidence),
        "failed_evidence": _failed_evidence(protocol)[:5],
    }


def _log_facts(log: ExecutionLog) -> dict[str, Any]:
    return {
        "phase": log.phase,
        "provider": log.provider,
        "exit_code": log.exit_code,
        "stderr": log.stderr[:800],
        "stdout": log.stdout[:800],
    }


def _acceptance_failure_reason(
    *,
    errors: list[str],
    checks: list[dict[str, Any]],
    run_phase: str,
    run_resolution: str,
    goal_phase: str,
    goal_resolution: str,
    protocol_completion: str,
    protocol_verification: str,
) -> str:
    del run_phase, run_resolution, goal_phase, goal_resolution, protocol_completion, protocol_verification
    if errors:
        return errors[0]
    failed = [check for check in checks if not check.get("ok")]
    if failed:
        first = failed[0]
        return f"{first.get('name')} failed: {first.get('error')}"
    return "product objective was not accepted"
