from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import load_config
from .governance import GovernanceEngine
from .evolution import EvolutionLedger
from .json_utils import parse_first_json_object
from .provider import ChatMessage, ModelPool, build_provider
from .tasks import Task, TaskStore

INTERNAL_EXECUTION_PROVIDER = "navi"
EXECUTION_PROTOCOL_VERSION = "navi.actuator.v1"
ACTUATOR_DISABLED_TOOLS = frozenset({"task.prepare", "task.queue", "execution.retry"})
EXECUTION_STEP_BUDGET = 5


@dataclass(frozen=True)
class ExecutionProtocol:
    version: str = EXECUTION_PROTOCOL_VERSION
    phase: str = ""
    task_id: str = ""
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
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "steps": self.steps,
            "evidence": self.evidence,
            "verification": self.verification,
            "completion": self.completion,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_model_output(cls, *, task_id: str, phase: str, text: str) -> "ExecutionProtocol":
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
        payload_task_id = str(payload.get("task_id") or task_id)
        if task_id and payload_task_id != task_id:
            raise ValueError("execution protocol task_id does not match task")
        steps = _required_steps(payload)
        evidence = _optional_dict_list(payload, "evidence")
        verification = _required_dict(payload, "verification")
        completion = _required_dict(payload, "completion")
        if not str(completion.get("summary") or "").strip():
            raise ValueError("execution protocol completion.summary is required")
        return cls(
            version=version,
            phase=payload_phase,
            task_id=payload_task_id,
            plan_id=str(payload.get("plan_id") or f"{phase}:{payload_task_id or 'watch'}"),
            steps=steps,
            evidence=evidence,
            verification=verification,
            completion=completion,
        )

    @classmethod
    def internal_status(
        cls,
        *,
        task_id: str,
        phase: str,
        status: str,
        summary: str,
        reason: str,
        action_kind: str,
    ) -> "ExecutionProtocol":
        return cls(
            phase=phase,
            task_id=task_id,
            plan_id=f"{phase}:{task_id or 'internal'}",
            steps=[
                {
                    "id": "internal",
                    "actions": [
                        {
                            "kind": action_kind,
                            "target": task_id,
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
        "Return one JSON object with key `navi_execution`. "
        f"`navi_execution.phase` must be `{phase}` and `version` must be `{EXECUTION_PROTOCOL_VERSION}`. "
        f"Use a step plan: include `plan_id` and `steps`, with at most {EXECUTION_STEP_BUDGET} steps. "
        "Each step must include `actions`, `verification`, and optional `on_failure` (`stop`, `continue`, or `retry_once`). "
        "Each step action must be a capability call object with `tool`, `permission`, and `args`; "
        "examples include `final.answer`, `provider.config`, `filesystem.list`, `file.read`, `file.write`, "
        "`shell.run`, `test.run`, `git.status`, `task.status`, "
        "`task.record`, `approval.request`, `approval.resolve`, `watch.create`, `task.delete`, and `watch.delete`. "
        "Do not describe an action that is not a capability call. "
        "Include `evidence` only as proposed context; Navi will replace it with actual capability results. "
        "Each step `verification.checks` must state expected checks. Supported object checks: "
        "`{\"type\":\"file.exists\",\"path\":\"...\"}`, `{\"type\":\"file.contains\",\"path\":\"...\",\"text\":\"...\"}`, "
        "`{\"type\":\"git.diff\",\"expected\":\"changed|clean\"}`, and `{\"type\":\"test.passed\"}`. "
        "`completion` may include a proposed summary, but final success is decided by capability execution."
    )


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

    @property
    def summary(self) -> str:
        if self.protocol.summary:
            return self.protocol.summary
        text = (self.stdout or self.stderr).strip()
        return text[:1600] if text else f"{self.phase} exited with {self.exit_code}"


class ActuatorRunner:
    def __init__(self, *, home: Path):
        self.home = home

    async def run_task(self, task: Task, result: ExecutionResult) -> ExecutionResult:
        workspace = Path(task.workspace or Path.cwd())
        before_state = _workspace_state(workspace, label="before")
        protocol = await self._execute_protocol_actions(task, result.protocol, before_state=before_state)
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
        task: Task,
        protocol: ExecutionProtocol,
        *,
        before_state: dict[str, Any],
    ) -> ExecutionProtocol:
        from .capabilities import CapabilityContext, CapabilityRegistry

        workspace = Path(task.workspace or Path.cwd())
        registry = CapabilityRegistry(
            home=self.home,
            project_dir=workspace,
            disabled_tools=set(ACTUATOR_DISABLED_TOOLS),
            permission_ceiling="write",
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
                evidence.append(_recovery_evidence(step_index, "retry_once", "retrying failed step once"))
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
            task_id=protocol.task_id,
            plan_id=protocol.plan_id,
            steps=acted_steps,
            evidence=evidence,
            verification={
                "status": "verified" if completed else "failed",
                "checks": [f"capability:{item.get('tool') or 'missing'}" for item in evidence if item.get("kind") == "capability_result"]
                + [item.get("check", "") for item in evidence if item.get("kind") == "verification_result"],
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
                acted_actions.append({**action, "status": "failed", "error": message, "attempt": attempt})
                step_evidence.append(_capability_evidence(step_index=step_index, index=action_index, tool="", ok=False, error=message, attempt=attempt))
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
                    task_id=result.task_id,
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
    task_id: str = "",
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
        "task_id": task_id,
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
        result = _run_verification_check(index=index, check=check, evidence=evidence, workspace=workspace, step_index=step_index, attempt=attempt)
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
    if check_type == "file.exists":
        path, error = _verified_workspace_path(check.get("path"), workspace=workspace)
        ok = bool(path and path.exists() and path.is_file())
        return _verification_evidence(step_index, index, check_type, ok=ok, facts={"path": str(path) if path else ""}, error=error or ("" if ok else "file does not exist"), attempt=attempt)
    if check_type == "file.contains":
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
            check_type,
            ok=ok,
            facts={"path": str(path) if path else "", "text_present": expected in content if expected else False},
            error=error or ("" if ok else "expected text not found"),
            attempt=attempt,
        )
    if check_type == "git.diff":
        expected = str(check.get("expected") or "changed").strip().lower()
        status = _git_porcelain_status(workspace)
        changed = bool(status["stdout"].strip())
        ok = (expected == "changed" and changed) or (expected == "clean" and not changed)
        return _verification_evidence(
            step_index,
            index,
            check_type,
            ok=ok and status["exit_code"] == 0,
            facts={"expected": expected, "changed": changed, **status},
            error=status["stderr"] or ("" if ok else f"git diff expectation not met: {expected}"),
            attempt=attempt,
        )
    if check_type == "test.passed":
        test_results = [item for item in evidence if item.get("tool") == "test.run"]
        ok = bool(test_results) and bool(test_results[-1].get("ok"))
        return _verification_evidence(
            step_index,
            index,
            check_type,
            ok=ok,
            facts={"test_result_count": len(test_results)},
            error="" if ok else "no passing test.run result found",
            attempt=attempt,
        )
    return _verification_evidence(step_index, index, check_type or "unknown", ok=False, error=f"unsupported verification check: {check_type or 'missing'}", attempt=attempt)


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
    return {"stdout": result.stdout[:12000], "stderr": result.stderr.strip()[:12000], "exit_code": result.returncode}


def _workspace_state(workspace: Path, *, label: str) -> dict[str, Any]:
    status = _git_porcelain_status(workspace)
    return {
        "kind": "workspace_state",
        "label": label,
        "workspace": str(workspace),
        "git": status,
        "dirty": bool(status["stdout"].strip()) if status["exit_code"] == 0 else None,
    }


def _recovery_evidence(step_index: int, policy: str, reason: str) -> dict[str, Any]:
    return {
        "kind": "recovery_decision",
        "step_index": step_index,
        "policy": policy,
        "reason": reason,
    }


def _rollback_evidence(before_state: dict[str, Any], after_state: dict[str, Any], failures: list[str]) -> dict[str, Any] | None:
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
        "suggested_action": "inspect git diff and revert or repair changed files before retrying",
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

    def __init__(self, *, provider: ModelPool, timeout_seconds: float):
        self.provider = provider
        self.timeout_seconds = timeout_seconds

    async def plan(self, task: Task) -> ExecutionResult:
        return await self._complete_task(task, phase="prepare", role="planner", messages=self._prepare_messages(task))

    async def execute(self, task: Task) -> ExecutionResult:
        return await self._complete_task(task, phase="execute", role="responder", messages=self._execute_messages(task))

    async def run_watch(self, *, prompt: str, source: str, peer_id: str, sender_id: str, workspace: str = "") -> ExecutionResult:
        messages = [
            ChatMessage(
                "system",
                "You are Navi running a scheduled watch. Complete the scheduled request directly. "
                "Do not create a task, ask for approval, or mention external execution tools. "
                + _execution_protocol_instruction("watch"),
            ),
            ChatMessage(
                "user",
                (
                    f"Watch source: {source}\n"
                    f"Peer id: {peer_id}\n"
                    f"Sender id: {sender_id}\n\n"
                    f"Workspace: {workspace or str(Path.home())}\n\n"
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
            return self._result(
                task_id="",
                phase="watch",
                provider=INTERNAL_EXECUTION_PROVIDER,
                command=["navi", "internal", "watch"],
                stdout=stdout,
                stderr="",
                exit_code=0,
                started_at=started,
                ended_at=time.time(),
            )
        except asyncio.TimeoutError:
            return self._result(
                task_id="",
                phase="watch",
                provider=INTERNAL_EXECUTION_PROVIDER,
                command=["navi", "internal", "watch"],
                stdout="",
                stderr=f"navi watch timed out after {self.timeout_seconds} seconds",
                exit_code=124,
                started_at=started,
                ended_at=time.time(),
            )
        except Exception as exc:
            return self._result(
                task_id="",
                phase="watch",
                provider=INTERNAL_EXECUTION_PROVIDER,
                command=["navi", "internal", "watch"],
                stdout="",
                stderr=str(exc),
                exit_code=1,
                started_at=started,
                ended_at=time.time(),
            )

    async def _complete_task(
        self,
        task: Task,
        *,
        phase: str,
        role: str,
        messages: list[ChatMessage],
    ) -> ExecutionResult:
        started = time.time()
        try:
            stdout = await asyncio.wait_for(
                self.provider.complete_for(role, messages),
                timeout=self.timeout_seconds,
            )
            return self._result(
                task_id=task.id,
                phase=phase,
                provider=INTERNAL_EXECUTION_PROVIDER,
                command=["navi", "internal", phase, task.id],
                stdout=stdout,
                stderr="",
                exit_code=0,
                started_at=started,
                ended_at=time.time(),
            )
        except asyncio.TimeoutError:
            return self._result(
                task_id=task.id,
                phase=phase,
                provider=INTERNAL_EXECUTION_PROVIDER,
                command=["navi", "internal", phase, task.id],
                stdout="",
                stderr=f"navi {phase} timed out after {self.timeout_seconds} seconds",
                exit_code=124,
                started_at=started,
                ended_at=time.time(),
            )
        except Exception as exc:
            return self._result(
                task_id=task.id,
                phase=phase,
                provider=INTERNAL_EXECUTION_PROVIDER,
                command=["navi", "internal", phase, task.id],
                stdout="",
                stderr=str(exc),
                exit_code=1,
                started_at=started,
                ended_at=time.time(),
            )

    @staticmethod
    def _result(
        *,
        task_id: str,
        provider: str,
        phase: str,
        command: list[str],
        stdout: str,
        stderr: str,
        exit_code: int,
        started_at: float,
        ended_at: float,
    ) -> ExecutionResult:
        protocol_text = stdout if stdout else stderr
        result_stderr = stderr
        result_exit_code = exit_code
        try:
            protocol = ExecutionProtocol.from_model_output(
                task_id=task_id,
                phase=phase,
                text=protocol_text,
            )
        except ValueError as exc:
            result_exit_code = 1
            result_stderr = str(exc)
            protocol = ExecutionProtocol.internal_status(
                task_id=task_id,
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
        )

    @staticmethod
    def _prepare_messages(task: Task) -> list[ChatMessage]:
        return [
            ChatMessage(
                "system",
                "You are Navi's internal preparation pass. Produce concise preparation facts: "
                "intent, risk, expected actions, affected local areas, and whether user approval is required. "
                "Do not call external CLI agents or claim files were changed. "
                + _execution_protocol_instruction("prepare"),
            ),
            ChatMessage(
                "user",
                (
                    f"Task id: {task.id}\n"
                    f"Workspace: {task.workspace or str(Path.home())}\n"
                    f"Autonomy level: {task.autonomy_level}\n\n"
                    f"Task:\n{task.prompt}"
                ),
            ),
        ]

    @staticmethod
    def _execute_messages(task: Task) -> list[ChatMessage]:
        return [
            ChatMessage(
                "system",
                "You are Navi's internal execution pass. Complete the approved task using Navi's own reasoning "
                "and available task context. Do not call external CLI agents. If the task requires OS mutation "
                "that this internal pass cannot perform, say exactly what remains unperformed. "
                + _execution_protocol_instruction("execute"),
            ),
            ChatMessage(
                "user",
                (
                    f"Task id: {task.id}\n"
                    f"Workspace: {task.workspace or str(Path.home())}\n"
                    f"Preparation summary:\n{task.plan_summary or '(none)'}\n\n"
                    f"Task:\n{task.prompt}"
                ),
            ),
        ]

    @staticmethod
    def mock_result(task: Task, phase: str, text: str) -> ExecutionResult:
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
                task_id=task.id,
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
                evidence=[{"kind": "mock_provider", "summary": text}],
                verification={"status": "proposed", "checks": ["mock provider response"], "reason": "execution mock mode"},
                completion={"status": "proposed", "summary": text},
            ),
        )


class ExecutionService:
    def __init__(self, home: Path):
        self.home = home
        config = load_config(home)
        self.config = config.execution
        self.tasks = TaskStore(home)
        self.governance = GovernanceEngine(home)
        self.ledger = EvolutionLedger(home)
        self.actuator = ActuatorRunner(home=home)
        self.provider = NaviExecutionProvider(
            provider=build_provider(config.model),
            timeout_seconds=self.config.timeout_seconds,
        )

    async def plan_task(self, task: Task) -> Task:
        self.tasks.update_task(task.id, status="preparing")
        result = await self._provider_call_with_timeout(task, "prepare")
        if result.exit_code == 0:
            result = await self.actuator.run_task(task, result)
        self._log(task, result)
        status = "prepared" if result.exit_code == 0 else "failed"
        return self.tasks.update_task(
            task.id,
            status=status,
            plan_summary=result.summary,
            error="" if result.exit_code == 0 else result.stderr,
        ) or task

    async def execute_task(self, task: Task) -> Task:
        # Record before state for rollback support
        task_before = self.tasks.get(task.id)
        before_state = json.dumps(
            {
                "status": task_before.status if task_before else "queued",
                "result_summary": task_before.result_summary if task_before else "",
                "error": task_before.error if task_before else "",
            },
            sort_keys=True
        )

        self.tasks.update_task(task.id, status="running")

        result = await self._provider_call_with_timeout(task, "execute")
        if result.exit_code == 0:
            result = await self.actuator.run_task(task, result)
        self._log(task, result)
        
        status = "completed" if result.exit_code == 0 else "failed"
        updated_task = self.tasks.update_task(
            task.id,
            status=status,
            result_summary=result.summary,
            error="" if result.exit_code == 0 else result.stderr,
        ) or task
        
        # Record the evolution event
        after_state = json.dumps(
            {
                "status": updated_task.status,
                "result_summary": updated_task.result_summary,
                "error": updated_task.error,
            },
            sort_keys=True
        )
        
        self.ledger.record(
            task_id=task.id,
            target_type="task_execution",
            target_id=task.id,
            reason=f"task execution {'completed' if result.exit_code == 0 else 'failed'}",
            before=before_state,
            after=after_state,
        )
        
        return updated_task

    async def run_watch(self, *, prompt: str, source: str, peer_id: str, sender_id: str, workspace: str = "") -> ExecutionResult:
        watch_task = Task(
            id="",
            title=prompt[:120],
            status="running",
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
        result = await self.provider.run_watch(
            prompt=prompt,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            workspace=workspace,
        )
        if result.exit_code == 0:
            result = await self.actuator.run_task(watch_task, result)
        self._log(watch_task, result)
        return result

    async def process_pending_once(self, *, limit: int = 3) -> list[Task]:
        completed: list[Task] = []
        for task in self.tasks.list_by_status("queued", limit=limit):
            if not self._execution_allowed(task):
                blocked = self.tasks.update_task(
                    task.id,
                    status="blocked",
                    error="execution grant missing: approved approval or explicit L3 trust rule required",
                )
                if blocked:
                    completed.append(blocked)
                continue
            completed.append(await self.execute_task(task))
        return completed

    def execution_allowed(self, task: Task) -> bool:
        return self._execution_allowed(task)

    def _execution_allowed(self, task: Task) -> bool:
        return self.governance.execution_allowed(task)

    def _log(self, task: Task, result: ExecutionResult) -> None:
        protocol = result.protocol
        self.tasks.add_execution_log(
            task_id=task.id,
            provider=result.provider,
            phase=result.phase,
            command=" ".join(result.command),
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            started_at=result.started_at,
            ended_at=result.ended_at,
        )
        self.tasks.add_execution_log(
            task_id=task.id,
            provider=result.provider,
            phase=f"{result.phase}_protocol",
            command=" ".join(["navi", "protocol", result.phase, task.id]),
            stdout=protocol.to_json(),
            stderr="",
            exit_code=result.exit_code,
            started_at=result.started_at,
            ended_at=result.ended_at,
        )

    async def _provider_call(self, task: Task, phase: str) -> ExecutionResult:
        if self.config.mock:
            text = (
                "Preparation: inspect the request, estimate risk, then request approval."
                if phase == "prepare"
                else f"Executed task internally: {task.prompt}"
            )
            return NaviExecutionProvider.mock_result(task, phase, text)
        if phase == "prepare":
            return await self.provider.plan(task)
        return await self.provider.execute(task)

    async def _provider_call_with_timeout(self, task: Task, phase: str) -> ExecutionResult:
        if self.config.mock:
            return await self._provider_call(task, phase)
        started = time.time()
        try:
            return await asyncio.wait_for(self._provider_call(task, phase), timeout=self.config.timeout_seconds + 1)
        except asyncio.TimeoutError:
            return ExecutionResult(
                provider=INTERNAL_EXECUTION_PROVIDER,
                phase=phase,
                command=["navi", "internal", "--timeout", phase, task.id],
                stdout="",
                stderr=f"navi {phase} timed out after {self.config.timeout_seconds} seconds",
                exit_code=124,
                started_at=started,
                ended_at=time.time(),
                protocol=ExecutionProtocol.internal_status(
                    task_id=task.id,
                    phase=phase,
                    status="failed",
                    summary=f"navi {phase} timed out after {self.config.timeout_seconds} seconds",
                    reason="execution provider timed out",
                    action_kind="execution_timeout",
                ),
            )
