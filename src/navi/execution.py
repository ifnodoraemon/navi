from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import load_config
from .governance import GovernanceEngine
from .evolution import EvolutionLedger
from .goals import GoalStore
from .json_utils import parse_first_json_object
from .lifecycle import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_PREPARING,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    execute_finalize_decision,
    execution_ledger_reason,
    prepare_run_status,
)
from .provider import ChatMessage, ModelPool, build_provider
from .runs import Run, RunStore
from .subagents import SubagentRunStore
from .tools import ACTUATOR_CONTEXT

INTERNAL_EXECUTION_PROVIDER = "navi"
EXECUTION_PROTOCOL_VERSION = "navi.actuator.v1"
EXECUTION_STEP_BUDGET = 5
SUBAGENT_PLANNER_ROLE = "planner"
SUBAGENT_EXECUTOR_ROLE = "executor"
SUBAGENT_NOTIFICATION_ROLE = "notification"


@dataclass(frozen=True)
class ExecutionProtocol:
    version: str = EXECUTION_PROTOCOL_VERSION
    phase: str = ""
    run_id: str = ""
    plan_id: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    completion: dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        summary = str(self.completion.get("summary") or "").strip()
        return summary[:1600]

    @property
    def actions(self) -> list[dict[str, Any]]:
        flattened: list[dict[str, Any]] = []
        for step in self.steps:
            if isinstance(step, dict):
                flattened.extend(item for item in step.get("actions", []) if isinstance(item, dict))
        return flattened

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "phase": self.phase,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "steps": self.steps,
            "evidence": self.evidence,
            "verification": self.verification,
            "completion": self.completion,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_model_output(cls, *, run_id: str, phase: str, text: str) -> "ExecutionProtocol":
        parsed = parse_first_json_object(text)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("navi_execution"), dict):
            raise ValueError("execution protocol missing navi_execution object")
        payload = parsed["navi_execution"]
        version = str(payload.get("version") or "")
        if version != EXECUTION_PROTOCOL_VERSION:
            raise ValueError(f"execution protocol version must be {EXECUTION_PROTOCOL_VERSION}")
        payload_phase = str(payload.get("phase") or "")
        if payload_phase != phase:
            raise ValueError(f"execution protocol phase must be {phase}")
        payload_run_id = str(payload.get("run_id") or run_id)
        if run_id and payload_run_id != run_id:
            raise ValueError("execution protocol run_id does not match task")
        payload = _normalize_execution_protocol_payload(payload, phase=phase)
        steps = _required_steps(payload)
        evidence = _optional_dict_list(payload, "evidence")
        verification = _required_dict(payload, "verification")
        completion = _required_dict(payload, "completion")
        if not str(completion.get("summary") or "").strip():
            raise ValueError("execution protocol completion.summary is required")
        return cls(
            version=version,
            phase=payload_phase,
            run_id=payload_run_id,
            plan_id=str(payload.get("plan_id") or f"{phase}:{payload_run_id or 'watch'}"),
            steps=steps,
            evidence=evidence,
            verification=verification,
            completion=completion,
        )

    @classmethod
    def internal_status(
        cls,
        *,
        run_id: str,
        phase: str,
        status: str,
        summary: str,
        reason: str,
        action_kind: str,
    ) -> "ExecutionProtocol":
        return cls(
            phase=phase,
            run_id=run_id,
            plan_id=f"{phase}:{run_id or 'internal'}",
            steps=[
                {
                    "id": "internal",
                    "actions": [
                        {
                            "kind": action_kind,
                            "target": run_id,
                            "status": status,
                            "summary": summary,
                        }
                    ],
                    "verification": {"checks": [], "reason": reason},
                    "status": status,
                }
            ],
            evidence=[
                {
                    "kind": "internal_state",
                    "summary": summary,
                }
            ],
            verification={
                "status": status,
                "checks": [],
                "reason": reason,
            },
            completion={
                "status": status,
                "summary": summary,
            },
        )


def _required_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"execution protocol {key} must be an object")
    return value


def _normalize_execution_protocol_payload(payload: dict[str, Any], *, phase: str) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["evidence"] = _normalize_evidence(normalized.get("evidence"))
    normalized["verification"] = _normalize_verification(
        normalized.get("verification"), reason="top-level verification"
    )
    normalized["completion"] = _normalize_completion(normalized.get("completion"))

    raw_steps = normalized.get("steps")
    if isinstance(raw_steps, list):
        steps: list[Any] = []
        for index, raw_step in enumerate(raw_steps):
            if not isinstance(raw_step, dict):
                steps.append(raw_step)
                continue
            step = dict(raw_step)
            actions = step.get("actions")
            if isinstance(actions, dict):
                step["actions"] = [actions]
            step["verification"] = _normalize_verification(
                step.get("verification"),
                reason=f"step {index + 1} verification",
                include_status=False,
            )
            steps.append(step)
        normalized["steps"] = steps

    if phase == "prepare":
        completion = normalized.get("completion")
        if isinstance(completion, dict) and not str(completion.get("status") or "").strip():
            completion["status"] = "proposed"
    return normalized


def _normalize_evidence(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [_normalize_evidence_item(value)]
    if isinstance(value, list):
        return [_normalize_evidence_item(item) for item in value[:20]]
    text = str(value).strip()
    return [{"kind": "model_context", "summary": text}] if text else []


def _normalize_evidence_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        normalized = dict(item)
        normalized.setdefault("kind", "model_context")
        if not str(normalized.get("summary") or "").strip():
            normalized["summary"] = json.dumps(item, ensure_ascii=False, sort_keys=True)[:1600]
        return normalized
    return {"kind": "model_context", "summary": str(item)[:1600]}


def _normalize_verification(
    value: Any, *, reason: str, include_status: bool = True
) -> dict[str, Any]:
    if isinstance(value, dict):
        normalized = dict(value)
        checks = normalized.get("checks")
        if checks is None:
            normalized["checks"] = []
        elif not isinstance(checks, list):
            normalized["checks"] = [checks]
        if include_status and not str(normalized.get("status") or "").strip():
            normalized["status"] = "proposed"
        normalized.setdefault("reason", reason)
        return normalized
    if isinstance(value, list):
        normalized = {"checks": value, "reason": f"{reason} normalized from list"}
        if include_status:
            normalized["status"] = "proposed"
        return normalized
    text = str(value or "").strip()
    normalized = {"checks": [text] if text else [], "reason": text or f"{reason} omitted"}
    if include_status:
        normalized["status"] = "proposed"
    return normalized


def _normalize_completion(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    text = str(value or "").strip()
    if not text:
        return {}
    return {"status": "proposed", "summary": text}


def _required_dict_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"execution protocol {key} must be a non-empty list")
    items = value[:20]
    if not all(isinstance(item, dict) for item in items):
        raise ValueError(f"execution protocol {key} entries must be objects")
    return items


def _optional_dict_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"execution protocol {key} must be a list")
    items = value[:20]
    if not all(isinstance(item, dict) for item in items):
        raise ValueError(f"execution protocol {key} entries must be objects")
    return items


def _required_steps(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("execution protocol steps must be a non-empty list")
    steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(raw_steps[:EXECUTION_STEP_BUDGET]):
        if not isinstance(raw_step, dict):
            raise ValueError("execution protocol step entries must be objects")
        actions = _required_dict_list(raw_step, "actions")
        verification = raw_step.get("verification")
        if not isinstance(verification, dict):
            raise ValueError("execution protocol step verification must be an object")
        steps.append(
            {
                "id": str(raw_step.get("id") or f"step-{index + 1}"),
                "description": str(raw_step.get("description") or ""),
                "actions": actions,
                "verification": verification,
                "on_failure": str(raw_step.get("on_failure") or "stop"),
            }
        )
    if len(raw_steps) > EXECUTION_STEP_BUDGET:
        raise ValueError(f"execution protocol exceeds step budget {EXECUTION_STEP_BUDGET}")
    return steps


def _execution_protocol_instruction(phase: str) -> str:
    return (
        f"Produce a {phase} step plan with at most {EXECUTION_STEP_BUDGET} steps. "
        "Each step action must be a capability call with a declared tool, permission, and args; "
        "examples include `final.answer`, `provider.config`, `directory.list`, `file.read`, `file.write`, "
        "`shell.run`, `test.run`, `git.status`, `delegate.status`, "
        "`delegate.spawn`, `approval.request`, `approval.resolve`, `watch.create`, `delegate.delete`, and `watch.delete`. "
        "Do not describe an action that is not a capability call. "
        "Include `evidence` only as proposed context; Navi will replace it with actual capability results. "
        "Verification checks should state expected checks. Supported object checks: "
        '`{"type":"file.exists","path":"..."}`, `{"type":"file.contains","path":"...","text":"..."}`, '
        '`{"type":"git.diff","expected":"changed|clean"}`, and `{"type":"test.passed"}`. '
        "Final success is decided by capability execution."
    )


def _execution_output_schema(phase: str) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "navi_execution": {
                "type": "object",
                "properties": {
                    "version": {"type": "string", "enum": [EXECUTION_PROTOCOL_VERSION]},
                    "phase": {"type": "string", "enum": [phase]},
                    "run_id": {"type": "string"},
                    "plan_id": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": EXECUTION_STEP_BUDGET,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "description": {"type": "string"},
                                "actions": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "tool": {"type": "string"},
                                            "permission": {
                                                "type": "string",
                                                "enum": ["read", "prepare", "write"],
                                            },
                                            "args": {"type": "object"},
                                        },
                                        "required": ["tool", "permission", "args"],
                                        "additionalProperties": True,
                                    },
                                },
                                "verification": {
                                    "type": "object",
                                    "properties": {
                                        "checks": {"type": "array"},
                                        "reason": {"type": "string"},
                                    },
                                    "required": ["checks", "reason"],
                                    "additionalProperties": True,
                                },
                                "on_failure": {
                                    "type": "string",
                                    "enum": ["stop", "continue", "retry_once"],
                                },
                            },
                            "required": ["id", "actions", "verification"],
                            "additionalProperties": True,
                        },
                    },
                    "evidence": {"type": "array", "items": {"type": "object"}},
                    "verification": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string"},
                            "checks": {"type": "array"},
                            "reason": {"type": "string"},
                        },
                        "required": ["status", "checks", "reason"],
                        "additionalProperties": True,
                    },
                    "completion": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                        "required": ["status", "summary"],
                        "additionalProperties": True,
                    },
                },
                "required": [
                    "version",
                    "phase",
                    "run_id",
                    "plan_id",
                    "steps",
                    "evidence",
                    "verification",
                    "completion",
                ],
                "additionalProperties": False,
            }
        },
        "required": ["navi_execution"],
        "additionalProperties": False,
    }
    return {"name": f"navi_{phase}_execution", "strict": False, "schema": schema}


@dataclass(frozen=True)
class ExecutionResult:
    provider: str
    phase: str
    command: list[str]
    stdout: str
    stderr: str
    exit_code: int
    started_at: float
    ended_at: float
    protocol: ExecutionProtocol
    model_role: str = ""

    @property
    def summary(self) -> str:
        if self.protocol.summary:
            return self.protocol.summary
        text = (self.stdout or self.stderr).strip()
        return text[:1600] if text else f"{self.phase} exited with {self.exit_code}"


class ActuatorRunner:
    def __init__(self, *, home: Path):
        self.home = home

    async def run_task(self, task: Run, result: ExecutionResult) -> ExecutionResult:
        workspace = _task_workspace(task)
        before_state = _workspace_state(workspace, label="before")
        protocol = await self._execute_protocol_actions(
            task, result.protocol, before_state=before_state
        )
        exit_code = 0 if protocol.completion.get("status") == "completed" else 1
        return ExecutionResult(
            provider=result.provider,
            phase=result.phase,
            command=[*result.command, "--actuated"],
            stdout=result.stdout,
            stderr="" if exit_code == 0 else protocol.summary,
            exit_code=exit_code,
            started_at=result.started_at,
            ended_at=time.time(),
            protocol=protocol,
        )

    async def _execute_protocol_actions(
        self,
        task: Run,
        protocol: ExecutionProtocol,
        *,
        before_state: dict[str, Any],
    ) -> ExecutionProtocol:
        from .capabilities import CapabilityContext, build_capability_registry

        workspace = _task_workspace(task)
        registry = build_capability_registry(
            home=self.home,
            project_dir=workspace,
            permission_ceiling="write",
            execution_context=ACTUATOR_CONTEXT,
        )
        context = CapabilityContext(
            home=self.home,
            peer_id=task.peer_id,
            sender_id=task.sender_id,
            source=task.source,
            permission_ceiling="write",
            workspace=task.workspace,
        )
        acted_steps: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = [before_state]
        failures: list[str] = []

        for step_index, step in enumerate(protocol.steps[:EXECUTION_STEP_BUDGET]):
            acted_step, step_evidence, step_failures = await self._execute_step(
                step,
                step_index=step_index,
                registry=registry,
                context=context,
                evidence=evidence,
                workspace=workspace,
            )
            acted_steps.append(acted_step)
            evidence.extend(step_evidence)
            if step_failures and step.get("on_failure") == "retry_once":
                evidence.append(
                    _recovery_evidence(step_index, "retry_once", "retrying failed step once")
                )
                retry_step, retry_evidence, retry_failures = await self._execute_step(
                    step,
                    step_index=step_index,
                    registry=registry,
                    context=context,
                    evidence=evidence,
                    workspace=workspace,
                    attempt=2,
                )
                acted_steps[-1] = {**retry_step, "previous_attempt": acted_step}
                evidence.extend(retry_evidence)
                step_failures = retry_failures
            if step_failures:
                failures.extend(step_failures)
                decision = str(step.get("on_failure") or "stop")
                evidence.append(_recovery_evidence(step_index, decision, "step failed"))
                if decision != "continue":
                    break

        if len(protocol.steps) > EXECUTION_STEP_BUDGET:
            failures.append(f"execution step budget exceeded: {EXECUTION_STEP_BUDGET}")

        after_state = _workspace_state(workspace, label="after")
        evidence.append(after_state)
        rollback = _rollback_evidence(before_state, after_state, failures)
        if rollback:
            evidence.append(rollback)
        completed = not failures
        summary = _actuator_summary(evidence, fallback=protocol.summary, failures=failures)
        return ExecutionProtocol(
            version=protocol.version,
            phase=protocol.phase,
            run_id=protocol.run_id,
            plan_id=protocol.plan_id,
            steps=acted_steps,
            evidence=evidence,
            verification={
                "status": "verified" if completed else "failed",
                "checks": [
                    f"capability:{item.get('tool') or 'missing'}"
                    for item in evidence
                    if item.get("kind") == "capability_result"
                ]
                + [
                    item.get("check", "")
                    for item in evidence
                    if item.get("kind") == "verification_result"
                ],
                "reason": "all protocol actions and verification checks passed"
                if completed
                else "one or more protocol actions or verification checks failed",
            },
            completion={
                "status": "completed" if completed else "failed",
                "summary": summary,
            },
        )

    async def _execute_step(
        self,
        step: dict[str, Any],
        *,
        step_index: int,
        registry,
        context,
        evidence: list[dict[str, Any]],
        workspace: Path,
        attempt: int = 1,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
        acted_actions: list[dict[str, Any]] = []
        step_evidence: list[dict[str, Any]] = []
        failures: list[str] = []
        for action_index, action in enumerate(step.get("actions", [])):
            tool = str(action.get("tool") or "").strip()
            permission = str(action.get("permission") or "read").strip() or "read"
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            if not tool:
                message = f"step {step_index + 1} action {action_index + 1} missing capability tool"
                failures.append(message)
                acted_actions.append(
                    {**action, "status": "failed", "error": message, "attempt": attempt}
                )
                step_evidence.append(
                    _capability_evidence(
                        step_index=step_index,
                        index=action_index,
                        tool="",
                        ok=False,
                        error=message,
                        attempt=attempt,
                    )
                )
                continue
            result = await registry.invoke(tool, args, permission=permission, context=context)
            if not result.ok:
                failures.append(result.message or result.observation or f"{tool} failed")
            acted_actions.append(
                {
                    **action,
                    "tool": tool,
                    "permission": permission,
                    "args": args,
                    "status": "completed" if result.ok else "failed",
                    "observation": result.observation,
                    "terminal": result.terminal,
                    "attempt": attempt,
                }
            )
            step_evidence.append(
                _capability_evidence(
                    step_index=step_index,
                    index=action_index,
                    tool=tool,
                    ok=result.ok,
                    action=result.action,
                    observation=result.observation,
                    message=result.message,
                    run_id=result.run_id,
                    terminal=result.terminal,
                    facts=result.facts or {},
                    attempt=attempt,
                )
            )
        verification_evidence, verification_failures = _verify_protocol_checks(
            step.get("verification", {}).get("checks"),
            evidence=[*evidence, *step_evidence],
            workspace=workspace,
            step_index=step_index,
            attempt=attempt,
        )
        step_evidence.extend(verification_evidence)
        failures.extend(verification_failures)
        return (
            {
                **step,
                "actions": acted_actions,
                "status": "completed" if not failures else "failed",
                "attempt": attempt,
            },
            step_evidence,
            failures,
        )


def _capability_evidence(
    *,
    step_index: int,
    index: int,
    tool: str,
    ok: bool,
    action: str = "",
    observation: str = "",
    message: str = "",
    run_id: str = "",
    terminal: bool = False,
    facts: dict[str, Any] | None = None,
    error: str = "",
    attempt: int = 1,
) -> dict[str, Any]:
    return {
        "kind": "capability_result",
        "step_index": step_index,
        "action_index": index,
        "attempt": attempt,
        "tool": tool,
        "ok": ok,
        "action": action,
        "observation": observation[:1600],
        "message": message[:1600],
        "run_id": run_id,
        "terminal": terminal,
        "facts": facts or {},
        "error": error,
    }


def _verify_protocol_checks(
    checks: Any,
    *,
    evidence: list[dict[str, Any]],
    workspace: Path,
    step_index: int,
    attempt: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(checks, list):
        return [], []
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            continue
        result = _run_verification_check(
            index=index,
            check=check,
            evidence=evidence,
            workspace=workspace,
            step_index=step_index,
            attempt=attempt,
        )
        results.append(result)
        if not result["ok"]:
            failures.append(str(result["error"] or f"verification check failed: {result['check']}"))
    return results, failures


def _run_verification_check(
    *,
    index: int,
    check: dict[str, Any],
    evidence: list[dict[str, Any]],
    workspace: Path,
    step_index: int,
    attempt: int,
) -> dict[str, Any]:
    check_type = str(check.get("type") or "").strip()
    handler = VERIFICATION_CHECK_HANDLERS.get(check_type)
    if handler is not None:
        return handler(
            check=check,
            evidence=evidence,
            workspace=workspace,
            step_index=step_index,
            index=index,
            attempt=attempt,
        )
    return _verification_evidence(
        step_index,
        index,
        check_type or "unknown",
        ok=False,
        error=f"unsupported verification check: {check_type or 'missing'}",
        attempt=attempt,
    )


VerificationCheckHandler = Callable[
    ...,
    dict[str, Any],
]


def _verify_file_exists(
    *,
    check: dict[str, Any],
    evidence: list[dict[str, Any]],
    workspace: Path,
    step_index: int,
    index: int,
    attempt: int,
) -> dict[str, Any]:
    del evidence
    path, error = _verified_workspace_path(check.get("path"), workspace=workspace)
    ok = bool(path and path.exists() and path.is_file())
    return _verification_evidence(
        step_index,
        index,
        "file.exists",
        ok=ok,
        facts={"path": str(path) if path else ""},
        error=error or ("" if ok else "file does not exist"),
        attempt=attempt,
    )


def _verify_file_contains(
    *,
    check: dict[str, Any],
    evidence: list[dict[str, Any]],
    workspace: Path,
    step_index: int,
    index: int,
    attempt: int,
) -> dict[str, Any]:
    del evidence
    path, error = _verified_workspace_path(check.get("path"), workspace=workspace)
    expected = str(check.get("text") or "")
    content = ""
    if path and path.exists() and path.is_file():
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            error = str(exc)
    ok = bool(path and expected and expected in content and not error)
    return _verification_evidence(
        step_index,
        index,
        "file.contains",
        ok=ok,
        facts={
            "path": str(path) if path else "",
            "text_present": expected in content if expected else False,
        },
        error=error or ("" if ok else "expected text not found"),
        attempt=attempt,
    )


def _verify_git_diff(
    *,
    check: dict[str, Any],
    evidence: list[dict[str, Any]],
    workspace: Path,
    step_index: int,
    index: int,
    attempt: int,
) -> dict[str, Any]:
    del evidence
    expected = str(check.get("expected") or "changed").strip().lower()
    status = _git_porcelain_status(workspace)
    changed = bool(status["stdout"].strip())
    ok = (expected == "changed" and changed) or (expected == "clean" and not changed)
    return _verification_evidence(
        step_index,
        index,
        "git.diff",
        ok=ok and status["exit_code"] == 0,
        facts={"expected": expected, "changed": changed, **status},
        error=status["stderr"] or ("" if ok else f"git diff expectation not met: {expected}"),
        attempt=attempt,
    )


def _verify_test_passed(
    *,
    check: dict[str, Any],
    evidence: list[dict[str, Any]],
    workspace: Path,
    step_index: int,
    index: int,
    attempt: int,
) -> dict[str, Any]:
    del check, workspace
    test_results = [item for item in evidence if item.get("tool") == "test.run"]
    ok = bool(test_results) and bool(test_results[-1].get("ok"))
    return _verification_evidence(
        step_index,
        index,
        "test.passed",
        ok=ok,
        facts={"test_result_count": len(test_results)},
        error="" if ok else "no passing test.run result found",
        attempt=attempt,
    )


VERIFICATION_CHECK_HANDLERS: dict[str, VerificationCheckHandler] = {
    "file.exists": _verify_file_exists,
    "file.contains": _verify_file_contains,
    "git.diff": _verify_git_diff,
    "test.passed": _verify_test_passed,
}


def _verification_evidence(
    step_index: int,
    index: int,
    check: str,
    *,
    ok: bool,
    facts: dict[str, Any] | None = None,
    error: str = "",
    attempt: int = 1,
) -> dict[str, Any]:
    return {
        "kind": "verification_result",
        "step_index": step_index,
        "check_index": index,
        "attempt": attempt,
        "check": check,
        "ok": ok,
        "facts": facts or {},
        "error": error,
    }


def _verified_workspace_path(value: Any, *, workspace: Path) -> tuple[Path | None, str]:
    raw = str(value or "").strip()
    if not raw:
        return None, "path is required"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = workspace / path
    try:
        resolved = path.resolve().absolute()
        root = workspace.resolve().absolute()
    except OSError as exc:
        return None, str(exc)
    if resolved != root and root not in resolved.parents:
        return None, "path must be within the workspace"
    return resolved, ""


def _git_porcelain_status(workspace: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except OSError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": 127}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "git status timed out", "exit_code": 124}
    return {
        "stdout": result.stdout[:12000],
        "stderr": result.stderr.strip()[:12000],
        "exit_code": result.returncode,
    }


def _workspace_state(workspace: Path, *, label: str) -> dict[str, Any]:
    status = _git_porcelain_status(workspace)
    return {
        "kind": "workspace_state",
        "label": label,
        "workspace": str(workspace),
        "git": status,
        "dirty": bool(status["stdout"].strip()) if status["exit_code"] == 0 else None,
    }


def _require_workspace_value(workspace: str, *, run_id: str = "") -> str:
    value = workspace.strip()
    if not value:
        suffix = f" for run {run_id}" if run_id else ""
        raise ValueError(f"workspace is required{suffix}")
    return value


def _task_workspace(task: Run) -> Path:
    return Path(_require_workspace_value(task.workspace, run_id=task.id))


def _recovery_evidence(step_index: int, policy: str, reason: str) -> dict[str, Any]:
    return {
        "kind": "recovery_decision",
        "step_index": step_index,
        "policy": policy,
        "reason": reason,
    }


def _rollback_evidence(
    before_state: dict[str, Any], after_state: dict[str, Any], failures: list[str]
) -> dict[str, Any] | None:
    if not failures:
        return None
    before_git = before_state.get("git") if isinstance(before_state.get("git"), dict) else {}
    after_git = after_state.get("git") if isinstance(after_state.get("git"), dict) else {}
    before_status = str(before_git.get("stdout") or "")
    after_status = str(after_git.get("stdout") or "")
    if before_status == after_status:
        return None
    return {
        "kind": "rollback_hint",
        "reason": "workspace changed during failed execution",
        "before_git_status": before_status,
        "after_git_status": after_status,
    }


def _actuator_summary(evidence: list[dict[str, Any]], *, fallback: str, failures: list[str]) -> str:
    if failures:
        return failures[0][:1600]
    for item in evidence:
        if item.get("message"):
            return str(item["message"])[:1600]
    for item in evidence:
        if item.get("observation"):
            return str(item["observation"])[:1600]
    return fallback[:1600] if fallback else "all protocol actions executed"


class NaviExecutionProvider:
    """Internal execution planner backed by Navi's configured model pool."""

    def __init__(self, *, provider: ModelPool, timeout_seconds: float, home: Path):
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.home = home

    async def plan(self, task: Run) -> ExecutionResult:
        return await self._complete_task(
            task, phase="prepare", role=SUBAGENT_PLANNER_ROLE, messages=self._prepare_messages(task)
        )

    async def execute(self, task: Run, previous_result: ExecutionResult | None = None) -> ExecutionResult:
        return await self._complete_task(
            task,
            phase="execute",
            role=SUBAGENT_EXECUTOR_ROLE,
            messages=self._execute_messages(task, previous_result=previous_result),
        )

    async def run_watch(
        self, *, prompt: str, source: str, peer_id: str, sender_id: str, workspace: str
    ) -> ExecutionResult:
        workspace = _require_workspace_value(workspace)
        messages = [
            ChatMessage(
                "system",
                "You are Navi running a scheduled watch. Complete the scheduled request directly. "
                "Return the exact notification text to send to the user. "
                "If the scheduled request is only a topic or title, expand it into a useful, self-contained notification "
                "with concrete content. Do not merely repeat the scheduled request. "
                "For learning or briefing requests, include a short title plus 3-5 concise sentences or bullets. "
                "Do not create a task, ask for approval, call tools, or mention external execution tools.",
            ),
            ChatMessage(
                "user",
                (
                    f"Watch source: {source}\n"
                    f"Peer id: {peer_id}\n"
                    f"Sender id: {sender_id}\n\n"
                    f"Workspace: {workspace}\n\n"
                    f"Scheduled request:\n{prompt}"
                ),
            ),
        ]
        started = time.time()
        try:
            stdout = await asyncio.wait_for(
                self.provider.complete_for("notification", messages),
                timeout=self.timeout_seconds,
            )
            if _is_title_only_watch_notification(stdout, prompt):
                retry_messages = [
                    *messages,
                    ChatMessage("assistant", stdout.strip()),
                    ChatMessage(
                        "user",
                        (
                            "The previous watch notification was only a title or repeated the request. "
                            "Rewrite it as the exact user-facing push message with substantive content. "
                            "Do not mention this correction."
                        ),
                    ),
                ]
                stdout = await asyncio.wait_for(
                    self.provider.complete_for("notification", retry_messages),
                    timeout=self.timeout_seconds,
                )
            return self._watch_result(
                provider=INTERNAL_EXECUTION_PROVIDER,
                command=["navi", "subagent", SUBAGENT_NOTIFICATION_ROLE, "watch"],
                stdout=stdout,
                stderr="",
                exit_code=0,
                started_at=started,
                ended_at=time.time(),
                model_role=SUBAGENT_NOTIFICATION_ROLE,
            )
        except asyncio.TimeoutError:
            return self._watch_result(
                provider=INTERNAL_EXECUTION_PROVIDER,
                command=["navi", "subagent", SUBAGENT_NOTIFICATION_ROLE, "watch"],
                stdout="",
                stderr=f"navi watch timed out after {self.timeout_seconds} seconds",
                exit_code=124,
                started_at=started,
                ended_at=time.time(),
                model_role=SUBAGENT_NOTIFICATION_ROLE,
            )
        except Exception as exc:
            return self._watch_result(
                provider=INTERNAL_EXECUTION_PROVIDER,
                command=["navi", "subagent", SUBAGENT_NOTIFICATION_ROLE, "watch"],
                stdout="",
                stderr=str(exc),
                exit_code=1,
                started_at=started,
                ended_at=time.time(),
                model_role=SUBAGENT_NOTIFICATION_ROLE,
            )

    async def _complete_task(
        self,
        task: Run,
        *,
        phase: str,
        role: str,
        messages: list[ChatMessage],
    ) -> ExecutionResult:
        started = time.time()
        try:
            stdout = await asyncio.wait_for(
                self.provider.complete_for(
                    role, messages, output_schema=_execution_output_schema(phase)
                ),
                timeout=self.timeout_seconds,
            )
            result = self._result(
                run_id=task.id,
                phase=phase,
                provider=INTERNAL_EXECUTION_PROVIDER,
                command=["navi", "subagent", role, phase, task.id],
                stdout=stdout,
                stderr="",
                exit_code=0,
                started_at=started,
                ended_at=time.time(),
                model_role=role,
            )
            if _should_retry_execution_protocol_result(result):
                repair_messages = [
                    *messages,
                    ChatMessage("assistant", stdout.strip()),
                    ChatMessage(
                        "user",
                        (
                            f"The previous response failed Navi's required execution protocol: {result.stderr}. "
                            "Revise it so the plan satisfies the same task and does not repeat that validation error."
                        ),
                    ),
                ]
                repaired_stdout = await asyncio.wait_for(
                    self.provider.complete_for(
                        role, repair_messages, output_schema=_execution_output_schema(phase)
                    ),
                    timeout=self.timeout_seconds,
                )
                return self._result(
                    run_id=task.id,
                    phase=phase,
                    provider=INTERNAL_EXECUTION_PROVIDER,
                    command=["navi", "subagent", role, phase, task.id, "--protocol-repair"],
                    stdout=repaired_stdout,
                    stderr="",
                    exit_code=0,
                    started_at=started,
                    ended_at=time.time(),
                    model_role=role,
                )
            return result
        except asyncio.TimeoutError:
            return self._result(
                run_id=task.id,
                phase=phase,
                provider=INTERNAL_EXECUTION_PROVIDER,
                command=["navi", "subagent", role, phase, task.id],
                stdout="",
                stderr=f"navi {phase} timed out after {self.timeout_seconds} seconds",
                exit_code=124,
                started_at=started,
                ended_at=time.time(),
                model_role=role,
            )
        except Exception as exc:
            return self._result(
                run_id=task.id,
                phase=phase,
                provider=INTERNAL_EXECUTION_PROVIDER,
                command=["navi", "subagent", role, phase, task.id],
                stdout="",
                stderr=str(exc),
                exit_code=1,
                started_at=started,
                ended_at=time.time(),
                model_role=role,
            )

    @staticmethod
    def _result(
        *,
        run_id: str,
        provider: str,
        phase: str,
        command: list[str],
        stdout: str,
        stderr: str,
        exit_code: int,
        started_at: float,
        ended_at: float,
        model_role: str = "",
    ) -> ExecutionResult:
        protocol_text = stdout if stdout else stderr
        result_stderr = stderr
        result_exit_code = exit_code
        try:
            protocol = ExecutionProtocol.from_model_output(
                run_id=run_id,
                phase=phase,
                text=protocol_text,
            )
        except ValueError as exc:
            result_exit_code = 1
            result_stderr = str(exc)
            protocol = ExecutionProtocol.internal_status(
                run_id=run_id,
                phase=phase,
                status="failed",
                summary=str(exc),
                reason="provider output violated the required execution protocol",
                action_kind="execution_error",
            )
        return ExecutionResult(
            provider=provider,
            phase=phase,
            command=command,
            stdout=stdout,
            stderr=result_stderr,
            exit_code=result_exit_code,
            started_at=started_at,
            ended_at=ended_at,
            protocol=protocol,
            model_role=model_role,
        )

    @staticmethod
    def _watch_result(
        *,
        provider: str,
        command: list[str],
        stdout: str,
        stderr: str,
        exit_code: int,
        started_at: float,
        ended_at: float,
        model_role: str = "",
    ) -> ExecutionResult:
        ok = exit_code == 0
        summary = (
            _watch_notification_summary(stdout)
            if ok
            else (stderr.strip() or f"watch exited with {exit_code}")
        )
        protocol = ExecutionProtocol.internal_status(
            run_id="",
            phase="watch",
            status="completed" if ok else "failed",
            summary=summary,
            reason="scheduled watch notification completed"
            if ok
            else "scheduled watch notification failed",
            action_kind="watch_notification",
        )
        return ExecutionResult(
            provider=provider,
            phase="watch",
            command=command,
            stdout=stdout,
            stderr="" if ok else stderr,
            exit_code=0 if ok else exit_code,
            started_at=started_at,
            ended_at=ended_at,
            protocol=protocol,
            model_role=model_role,
        )

    def _prepare_messages(self, task: Run) -> list[ChatMessage]:
        from .capabilities import CapabilityRegistry
        import json
        from dataclasses import asdict

        registry = CapabilityRegistry(
            home=self.home,
            project_dir=_task_workspace(task),
            permission_ceiling="write",
            execution_context=ACTUATOR_CONTEXT,
        )
        tools = registry.planner_specs(permission_ceiling="write")
        manifest_json = json.dumps([asdict(t) for t in tools], ensure_ascii=False)

        return [
            ChatMessage(
                "system",
                "You are Navi's internal preparation pass. Produce concise preparation facts: "
                "intent, risk, expected actions, affected local areas, and whether user approval is required. "
                "Do not call external CLI agents or claim files were changed. "
                + _execution_protocol_instruction("prepare")
                + f"\n\nCapability Manifest:\n{manifest_json}",
            ),
            ChatMessage(
                "user",
                (
                    f"Run id: {task.id}\n"
                    f"Workspace: {_task_workspace(task)}\n"
                    f"Autonomy level: {task.autonomy_level}\n\n"
                    f"Run:\n{task.prompt}"
                ),
            ),
        ]

    def _execute_messages(self, task: Run, previous_result: ExecutionResult | None = None) -> list[ChatMessage]:
        from .capabilities import CapabilityRegistry
        import json
        from dataclasses import asdict

        registry = CapabilityRegistry(
            home=self.home,
            project_dir=_task_workspace(task),
            permission_ceiling="write",
            execution_context=ACTUATOR_CONTEXT,
        )
        tools = registry.planner_specs(permission_ceiling="write")
        manifest_json = json.dumps([asdict(t) for t in tools], ensure_ascii=False)

        messages = [
            ChatMessage(
                "system",
                "You are Navi's internal execution pass. Complete the approved task using Navi's own reasoning "
                "as the executor sub-agent with isolated role context. Do not call external CLI agents. If the task requires OS mutation "
                "that this internal pass cannot perform, say exactly what remains unperformed. "
                + _execution_protocol_instruction("execute")
                + f"\n\nCapability Manifest:\n{manifest_json}",
            ),
            ChatMessage(
                "user",
                (
                    f"Run id: {task.id}\n"
                    f"Workspace: {_task_workspace(task)}\n"
                    f"Preparation summary:\n{task.plan_summary or '(none)'}\n\n"
                    f"Run:\n{task.prompt}"
                ),
            ),
        ]
        if previous_result:
            messages.append(ChatMessage("assistant", previous_result.stdout))
            messages.append(
                ChatMessage(
                    "user",
                    f"The execution of the previous plan failed:\n{previous_result.stderr}\n\nPlease analyze the failure and generate a new execution protocol to recover and complete the task."
                )
            )
        return messages

    @staticmethod
    def mock_result(task: Run, phase: str, text: str) -> ExecutionResult:
        now = time.time()
        return ExecutionResult(
            provider=INTERNAL_EXECUTION_PROVIDER,
            phase=phase,
            command=["navi", "internal", "--mock", phase, task.id],
            stdout=text,
            stderr="",
            exit_code=0,
            started_at=now,
            ended_at=now,
            protocol=ExecutionProtocol(
                run_id=task.id,
                phase=phase,
                plan_id=f"{phase}:{task.id}",
                steps=[
                    {
                        "id": "mock",
                        "actions": [
                            {
                                "tool": "final.answer",
                                "permission": "read",
                                "args": {"message": text},
                            }
                        ],
                        "verification": {"checks": [], "reason": "execution mock mode"},
                        "on_failure": "stop",
                    }
                ],
                evidence=[
                    {"kind": "capability_result", "ok": True, "tool": "final.answer", "summary": text},
                    {"kind": "verification_result", "ok": True, "check": "mock provider check"},
                ],
                verification={
                    "status": "verified",
                    "checks": ["mock provider check"],
                    "reason": "execution mock mode",
                },
                completion={"status": "completed", "summary": text},
            ),
            model_role="mock",
        )


def _watch_notification_summary(text: str) -> str:
    raw = text.strip()
    if not raw:
        return "scheduled watch completed"
    try:
        protocol = ExecutionProtocol.from_model_output(run_id="", phase="watch", text=raw)
    except ValueError:
        return raw[:1600]
    return _watch_protocol_notification_text(protocol) or protocol.summary or raw[:1600]


def _watch_protocol_notification_text(protocol: ExecutionProtocol) -> str:
    summary = (protocol.summary or "").strip()
    final_messages: list[str] = []
    for step in protocol.steps:
        actions = step.get("actions") if isinstance(step, dict) else None
        if not isinstance(actions, list):
            continue
        for action in actions:
            if not isinstance(action, dict) or action.get("tool") != "final.answer":
                continue
            args = action.get("args")
            if not isinstance(args, dict):
                continue
            message = str(args.get("message") or "").strip()
            if message:
                final_messages.append(message)
    if not final_messages:
        return ""
    best = max(final_messages, key=len)
    if not summary or len(best) > max(len(summary) + 8, 24):
        return best[:1600]
    return summary[:1600]


def _is_title_only_watch_notification(output: str, prompt: str) -> bool:
    summary = _watch_notification_summary(output)
    normalized_summary = _compact_watch_text(summary)
    normalized_prompt = _compact_watch_text(prompt)
    if not normalized_summary:
        return True
    if normalized_summary == normalized_prompt:
        return True
    visible_chars = [char for char in summary.strip() if not char.isspace()]
    if (
        len(visible_chars) <= 12
        and "\n" not in summary
        and "。" not in summary
        and "." not in summary
    ):
        return True
    return False


def _compact_watch_text(text: str) -> str:
    return "".join(str(text or "").split()).strip("。.!！?:：")


def _should_retry_execution_protocol_result(result: ExecutionResult) -> bool:
    return result.exit_code != 0 and result.stderr.startswith("execution protocol ")


class ExecutionService:
    def __init__(self, home: Path):
        self.home = home
        config = load_config(home)
        self.config = config.execution
        self.runs = RunStore(home)
        self.governance = GovernanceEngine(home)
        self.ledger = EvolutionLedger(home)
        self.actuator = ActuatorRunner(home=home)
        self.subagents = SubagentRunStore(home)
        self.provider = NaviExecutionProvider(
            provider=build_provider(config.model),
            timeout_seconds=self.config.timeout_seconds,
            home=home,
        )

    async def plan_task(self, task: Run) -> Run:
        self.runs.update_run(task.id, status=RUN_STATUS_PREPARING)
        result = await self._provider_call_with_timeout(task, "prepare")
        if result.exit_code == 0:
            result = await self.actuator.run_task(task, result)
        self._log(task, result)
        task_after_actuator = self.runs.get(task.id)
        status = prepare_run_status(
            exit_code=result.exit_code,
            current_status=task_after_actuator.status if task_after_actuator else "",
        )
        updated = (
            self.runs.update_run(
                task.id,
                status=status,
                plan_summary=result.summary,
                error="" if result.exit_code == 0 else result.stderr,
            )
            or task
        )
        GoalStore(self.home).update_for_run(
            updated,
            evidence={"run_id": updated.id, "run_status": updated.status, "phase": "prepare"},
        )
        return updated

    async def execute_task(self, task: Run) -> Run:
        before_state = self._execution_before_state(task)
        self.runs.update_run(task.id, status=RUN_STATUS_RUNNING)
        result = await self._provider_call_with_timeout(task, "execute")
        return self._finalize_execution_result(
            task,
            result,
            before_state=before_state,
            reason_exit_code=result.exit_code,
        )

    async def execute_actuator_protocol(self, task: Run, provider_result: ExecutionResult) -> Run:
        before_state = self._execution_before_state(task)
        self.runs.update_run(task.id, status=RUN_STATUS_RUNNING)
        result = (
            await self.actuator.run_task(task, provider_result)
            if provider_result.exit_code == 0
            else provider_result
        )
        return self._finalize_execution_result(
            task,
            result,
            before_state=before_state,
            reason_exit_code=result.exit_code,
        )

    def _execution_before_state(self, task: Run) -> str:
        task_before = self.runs.get(task.id)
        return json.dumps(
            {
                "status": task_before.status if task_before else "queued",
                "result_summary": task_before.result_summary if task_before else "",
                "error": task_before.error if task_before else "",
            },
            sort_keys=True,
        )

    def _finalize_execution_result(
        self,
        task: Run,
        result: ExecutionResult,
        *,
        before_state: str,
        reason_exit_code: int,
    ) -> Run:
        self._log(task, result)

        finalize = execute_finalize_decision(
            exit_code=result.exit_code,
            stderr=result.stderr,
        )
        updated_task = (
            self.runs.update_run(
                task.id,
                status=finalize.status,
                result_summary=result.summary,
                error=finalize.error,
            )
            or task
        )

        # Record the evolution event
        after_state = json.dumps(
            {
                "status": updated_task.status,
                "result_summary": updated_task.result_summary,
                "error": updated_task.error,
            },
            sort_keys=True,
        )

        self.ledger.record(
            run_id=task.id,
            target_type="run_execution",
            target_id=task.id,
            reason=execution_ledger_reason(reason_exit_code),
            before=before_state,
            after=after_state,
        )
        GoalStore(self.home).update_for_run(
            updated_task,
            evidence={
                "run_id": updated_task.id,
                "run_status": updated_task.status,
                "phase": "execute",
                "summary": updated_task.result_summary,
            },
        )

        return updated_task

    async def run_watch(
        self, *, prompt: str, source: str, peer_id: str, sender_id: str, workspace: str
    ) -> ExecutionResult:
        workspace = _require_workspace_value(workspace)
        watch_task = Run(
            id="",
            title=prompt[:120],
            status=RUN_STATUS_RUNNING,
            created_at=time.time(),
            updated_at=time.time(),
            kind="watch",
            prompt=prompt,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            provider=self.config.provider,
            workspace=workspace,
        )
        subagent_run = self.subagents.start(
            role=SUBAGENT_NOTIFICATION_ROLE,
            phase="watch",
            run_id="",
            command=["navi", "subagent", SUBAGENT_NOTIFICATION_ROLE, "watch"],
            input_data={
                "source": source,
                "peer_id": peer_id,
                "sender_id": sender_id,
                "workspace": workspace,
            },
        )
        result = await self.provider.run_watch(
            prompt=prompt,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            workspace=workspace,
        )
        self._log(watch_task, result)
        self.subagents.finish(
            subagent_run.id,
            status="completed" if result.exit_code == 0 else "failed",
            output_data={
                "exit_code": result.exit_code,
                "summary": result.summary,
                "provider": result.provider,
                "model_role": result.model_role,
            },
            error=result.stderr,
        )
        return result

    async def process_pending_once(self, *, limit: int = 3) -> list[Run]:
        completed: list[Run] = []
        for task in self.runs.list_by_status(RUN_STATUS_QUEUED, limit=limit):
            if not self._execution_allowed(task):
                blocked = self.runs.update_run(
                    task.id,
                    status=RUN_STATUS_BLOCKED,
                    error="execution grant missing: approved approval or explicit L3 trust rule required",
                )
                if blocked:
                    GoalStore(self.home).update_for_run(
                        blocked, evidence={"run_id": blocked.id, "run_status": blocked.status}
                    )
                    completed.append(blocked)
                continue
            completed.append(await self.execute_task(task))
        return completed

    def execution_allowed(self, task: Run) -> bool:
        return self._execution_allowed(task)

    def _execution_allowed(self, task: Run) -> bool:
        return self.governance.execution_allowed(task)

    def _log(self, task: Run, result: ExecutionResult) -> None:
        protocol = result.protocol
        self.runs.add_execution_log(
            run_id=task.id,
            provider=result.provider,
            phase=result.phase,
            command=" ".join(result.command),
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            started_at=result.started_at,
            ended_at=result.ended_at,
        )
        self.runs.add_execution_log(
            run_id=task.id,
            provider=result.provider,
            phase=f"{result.phase}_protocol",
            command=" ".join(["navi", "protocol", result.phase, task.id]),
            stdout=protocol.to_json(),
            stderr="",
            exit_code=result.exit_code,
            started_at=result.started_at,
            ended_at=result.ended_at,
        )



    async def _provider_call(self, task: Run, phase: str, previous_result: ExecutionResult | None = None) -> ExecutionResult:
        if self.config.mock and getattr(self, "_force_mock_result", True) and not getattr(self, "_test_disable_provider_mock", False):
            # Tests might set _test_disable_provider_mock to True to test ReActRunner fallback.
            text = (
                "Preparation: inspect the request, estimate risk, then request approval."
                if phase == "prepare"
                else f"Executed task internally: {task.prompt}"
            )
            return NaviExecutionProvider.mock_result(task, phase, text)
        if phase == "prepare":
            return await self.provider.plan(task)

        from .react_runner import ReActRunner
        react_runner = ReActRunner(home=self.home, provider=self.provider.provider)
        return await react_runner.run_task(task)

    async def _provider_call_with_timeout(self, task: Run, phase: str, previous_result: ExecutionResult | None = None) -> ExecutionResult:
        role = SUBAGENT_PLANNER_ROLE if phase == "prepare" else SUBAGENT_EXECUTOR_ROLE
        subagent_run = self.subagents.start(
            role=role,
            phase=phase,
            run_id=task.id,
            command=["navi", "subagent", role, phase, task.id],
            input_data={"workspace": task.workspace, "autonomy_level": task.autonomy_level},
        )
        if self.config.mock:
            result = await self._provider_call(task, phase, previous_result=previous_result)
            self._finish_provider_subagent(subagent_run.id, result)
            return result
        started = time.time()
        try:
            result = await self._provider_call(task, phase, previous_result=previous_result)
            self._finish_provider_subagent(subagent_run.id, result)
            return result
        except Exception as exc:
            result = ExecutionResult(
                provider=INTERNAL_EXECUTION_PROVIDER,
                phase=phase,
                command=["navi", "internal", "--error", phase, task.id],
                stdout="",
                stderr=repr(exc),
                exit_code=1,
                started_at=started,
                ended_at=time.time(),
                protocol=ExecutionProtocol.internal_status(
                    run_id=task.id,
                    phase=phase,
                    status="failed",
                    summary=repr(exc),
                    reason="execution provider raised an unexpected error",
                    action_kind="execution_error",
                ),
            )
            self._finish_provider_subagent(subagent_run.id, result)
            return result

    def _finish_provider_subagent(self, subagent_id: str, result: ExecutionResult) -> None:
        self.subagents.finish(
            subagent_id,
            status="completed" if result.exit_code == 0 else "failed",
            output_data={
                "exit_code": result.exit_code,
                "summary": result.summary,
                "provider": result.provider,
                "model_role": result.model_role,
            },
            error=result.stderr,
        )
