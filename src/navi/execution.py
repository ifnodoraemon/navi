from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .capabilities import CapabilityRegistry
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
from .prompting import PromptLayerStore

INTERNAL_EXECUTION_PROVIDER = "navi"
EXECUTION_PROTOCOL_VERSION = "navi.actuator.v1"
EXECUTION_STEP_BUDGET = 5
SUBAGENT_PLANNER_ROLE = "planner"
SUBAGENT_EXECUTOR_ROLE = "executor"
SUBAGENT_NOTIFICATION_ROLE = "notification"


_engine_class = None


def register_engine_class(cls: type) -> None:
    global _engine_class
    _engine_class = cls


def get_engine_class() -> type:
    if _engine_class is None:
        raise RuntimeError("HernessEngine class has not been registered yet.")
    return _engine_class


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
        return str(self.completion.get("summary") or "").strip()[:1600]

    @property
    def actions(self) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for step in self.steps:
            raw_actions = step.get("actions") if isinstance(step, dict) else None
            if isinstance(raw_actions, list):
                actions.extend(action for action in raw_actions if isinstance(action, dict))
        return actions

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
        if not isinstance(parsed, dict):
            raise ValueError("execution protocol missing JSON object")
        payload = (
            parsed.get("navi_execution")
            if isinstance(parsed.get("navi_execution"), dict)
            else parsed
        )
        version = str(payload.get("version") or "")
        if version != EXECUTION_PROTOCOL_VERSION:
            raise ValueError(f"execution protocol version must be {EXECUTION_PROTOCOL_VERSION}")
        payload_phase = str(payload.get("phase") or phase)
        if payload_phase != phase:
            raise ValueError(f"execution protocol phase must be {phase}")
        payload_run_id = str(payload.get("run_id") or run_id)
        if run_id and payload_run_id != run_id:
            raise ValueError("execution protocol run_id does not match task")
        steps = _protocol_steps(payload)
        completion = _protocol_dict(payload, "completion")
        summary = str(completion.get("summary") or "").strip()
        if not summary:
            raise ValueError("execution protocol completion.summary is required")
        return cls(
            version=version,
            phase=payload_phase,
            run_id=payload_run_id,
            plan_id=str(payload.get("plan_id") or f"{phase}:{payload_run_id or 'watch'}"),
            steps=steps,
            evidence=_protocol_dict_list(payload.get("evidence")),
            verification=_protocol_dict(payload, "verification"),
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
            evidence=[{"kind": "internal_state", "summary": summary}],
            verification={"status": status, "checks": [], "reason": reason},
            completion={"status": status, "summary": summary},
        )


def _protocol_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if isinstance(value, dict):
        return dict(value)
    if key == "verification":
        return {"status": "proposed", "checks": [], "reason": "verification omitted"}
    raise ValueError(f"execution protocol {key} must be an object")


def _protocol_dict_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("execution protocol evidence must be a list")
    return [dict(item) for item in value[:20] if isinstance(item, dict)]


def _protocol_steps(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("execution protocol steps must be a non-empty list")
    steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(raw_steps[:EXECUTION_STEP_BUDGET]):
        if not isinstance(raw_step, dict):
            raise ValueError("execution protocol step entries must be objects")
        raw_actions = raw_step.get("actions")
        actions = raw_actions if isinstance(raw_actions, list) else []
        if not actions:
            raise ValueError("execution protocol step actions must be a non-empty list")
        verification = raw_step.get("verification")
        steps.append(
            {
                "id": str(raw_step.get("id") or f"step-{index + 1}"),
                "description": str(raw_step.get("description") or ""),
                "actions": [dict(action) for action in actions if isinstance(action, dict)],
                "verification": dict(verification) if isinstance(verification, dict) else {},
                "on_failure": str(raw_step.get("on_failure") or "stop"),
            }
        )
    if len(raw_steps) > EXECUTION_STEP_BUDGET:
        raise ValueError(f"execution protocol exceeds step budget {EXECUTION_STEP_BUDGET}")
    return steps


def _execution_protocol_instruction(phase: str) -> str:
    return (
        f"Return a {phase} protocol with at most {EXECUTION_STEP_BUDGET} steps. "
        "Each step action must name a declared capability tool, permission, and args. "
        "The protocol is declarative evidence for Navi; do not claim side effects that did not run."
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
    protocol: ExecutionProtocol | None = None
    model_role: str = ""

    @property
    def summary(self) -> str:
        if self.protocol and self.protocol.summary:
            return self.protocol.summary
        text = (self.stdout or self.stderr).strip()
        return text[:1600] if text else f"{self.phase} exited with {self.exit_code}"


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
            task,
            phase="prepare",
            role=SUBAGENT_PLANNER_ROLE,
            messages=self._prepare_messages(task),
        )

    async def run_watch(
        self, *, prompt: str, source: str, peer_id: str, sender_id: str, workspace: str
    ) -> ExecutionResult:
        workspace = _require_workspace_value(workspace)
        watch_prompt = PromptLayerStore(self.home).read("execution_watch")
        messages = [
            ChatMessage(
                "system",
                watch_prompt,
            ),
            ChatMessage(
                "user",
                (
                    f"Watch source: {source}\n"
                    f"Peer id: {peer_id}\n"
                    f"Sender id: {sender_id}\n"
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
            return self._watch_result(
                stdout=stdout,
                stderr="",
                exit_code=0,
                started_at=started,
                ended_at=time.time(),
            )
        except asyncio.TimeoutError:
            return self._watch_result(
                stdout="",
                stderr=f"navi watch timed out after {self.timeout_seconds} seconds",
                exit_code=124,
                started_at=started,
                ended_at=time.time(),
            )
        except Exception as exc:
            return self._watch_result(
                stdout="",
                stderr=str(exc),
                exit_code=1,
                started_at=started,
                ended_at=time.time(),
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
                    role,
                    messages,
                    output_schema=_execution_output_schema(phase),
                ),
                timeout=self.timeout_seconds,
            )
            return self._result(
                run_id=task.id,
                phase=phase,
                command=["navi", "subagent", role, phase, task.id],
                stdout=stdout,
                stderr="",
                exit_code=0,
                started_at=started,
                ended_at=time.time(),
                model_role=role,
            )
        except asyncio.TimeoutError:
            return self._result(
                run_id=task.id,
                phase=phase,
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
                command=["navi", "subagent", role, phase, task.id],
                stdout="",
                stderr=str(exc),
                exit_code=1,
                started_at=started,
                ended_at=time.time(),
                model_role=role,
            )

    def _prepare_messages(self, task: Run) -> list[ChatMessage]:
        registry = CapabilityRegistry(
            home=self.home,
            project_dir=_task_workspace(task),
            permission_ceiling="write",
            execution_context=ACTUATOR_CONTEXT,
        )
        manifest = json.dumps(
            [asdict(spec) for spec in registry.planner_specs(permission_ceiling="write")],
            ensure_ascii=False,
        )
        prepare_prompt = PromptLayerStore(self.home).read("execution_prepare").strip() + " "
        return [
            ChatMessage(
                "system",
                (
                    prepare_prompt
                    + _execution_protocol_instruction("prepare")
                    + f"\n\nCapability Manifest:\n{manifest}"
                ),
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



    @staticmethod
    def _result(
        *,
        run_id: str,
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
            provider=INTERNAL_EXECUTION_PROVIDER,
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
        stdout: str,
        stderr: str,
        exit_code: int,
        started_at: float,
        ended_at: float,
    ) -> ExecutionResult:
        ok = exit_code == 0
        summary = (
            stdout.strip()[:1600] if ok else (stderr.strip() or f"watch exited with {exit_code}")
        )
        protocol = ExecutionProtocol.internal_status(
            run_id="",
            phase="watch",
            status="completed" if ok else "failed",
            summary=summary or "scheduled watch completed",
            reason="scheduled watch notification completed" if ok else "scheduled watch failed",
            action_kind="watch_notification",
        )
        return ExecutionResult(
            provider=INTERNAL_EXECUTION_PROVIDER,
            phase="watch",
            command=["navi", "subagent", SUBAGENT_NOTIFICATION_ROLE, "watch"],
            stdout=stdout,
            stderr="" if ok else stderr,
            exit_code=0 if ok else exit_code,
            started_at=started_at,
            ended_at=ended_at,
            protocol=protocol,
            model_role=SUBAGENT_NOTIFICATION_ROLE,
        )


class ExecutionService:
    def __init__(self, home: Path):
        self.home = home
        config = load_config(home)
        self.config = config.execution
        self.runs = RunStore(home)
        self.governance = GovernanceEngine(home)
        self.ledger = EvolutionLedger(home)
        self.subagents = SubagentRunStore(home)
        self.provider = NaviExecutionProvider(
            provider=build_provider(config.model),
            timeout_seconds=self.config.timeout_seconds,
            home=home,
        )

    async def plan_task(self, task: Run) -> Run:
        self.runs.update_run(task.id, status=RUN_STATUS_PREPARING)
        result = await self._prepare_with_subagent(task)
        self._log(task, result)
        task_after_prepare = self.runs.get(task.id)
        status = prepare_run_status(
            exit_code=result.exit_code,
            current_status=task_after_prepare.status if task_after_prepare else "",
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

        from .runtime import AgentRuntime

        HernessEngine = get_engine_class()

        config = load_config(self.home)
        runtime = AgentRuntime(home=self.home, provider=build_provider(config.model))

        permission_ceiling = "write" if self.execution_allowed(task) else "prepare"

        started_at = time.time()
        subagent_run = self.subagents.start(
            role=SUBAGENT_EXECUTOR_ROLE,
            phase="execute",
            run_id=task.id,
            command=["navi", "subagent", SUBAGENT_EXECUTOR_ROLE, "execute", task.id],
            input_data={"workspace": task.workspace, "autonomy_level": task.autonomy_level},
        )
        engine = HernessEngine(
            home=self.home,
            runtime=runtime,
            project_dir=_task_workspace(task),
            permission_ceiling=permission_ceiling,
            disabled_capability_classes=frozenset({"delegate"}),
            step_budget=15,
        )
        turn_result = await engine.handle(
            f"Execute the following task:\n\n{task.prompt}\n\nWhen finished, synthesize a final completion summary.",
            peer_id=task.peer_id,
            sender_id=task.sender_id,
            source=task.source,
            session_alias=f"executor:{task.id}",
        )
        exit_code = 0 if not turn_result.budget_exhausted else 1
        execution_status = "completed" if exit_code == 0 else "failed"
        self.subagents.finish(
            subagent_run.id,
            status=execution_status,
            output_data={
                "exit_code": exit_code,
                "summary": turn_result.text,
                "provider": "react",
                "model_role": turn_result.model_role,
                "trace_id": turn_result.trace_id,
            },
            error=turn_result.text if exit_code != 0 else "",
        )
        protocol = ExecutionProtocol.internal_status(
            run_id=task.id,
            phase="execute",
            status=execution_status,
            summary=turn_result.text[:1600] if turn_result.text else "",
            reason="HernessEngine execution finished",
            action_kind="herness_engine",
        )
        result = ExecutionResult(
            provider="react",
            phase="execute",
            command=["navi", "react", task.id],
            stdout=turn_result.text,
            stderr=turn_result.text if exit_code != 0 else "",
            exit_code=exit_code,
            started_at=started_at,
            ended_at=time.time(),
            protocol=protocol,
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
        if result.protocol is None:
            return
        self.runs.add_execution_log(
            run_id=task.id,
            provider=result.provider,
            phase=f"{result.phase}_protocol",
            command=" ".join(["navi", "protocol", result.phase, task.id]),
            stdout=result.protocol.to_json(),
            stderr="",
            exit_code=result.exit_code,
            started_at=result.started_at,
            ended_at=result.ended_at,
        )

    async def _prepare_with_subagent(self, task: Run) -> ExecutionResult:
        subagent_run = self.subagents.start(
            role=SUBAGENT_PLANNER_ROLE,
            phase="prepare",
            run_id=task.id,
            command=["navi", "subagent", SUBAGENT_PLANNER_ROLE, "prepare", task.id],
            input_data={"workspace": task.workspace, "autonomy_level": task.autonomy_level},
        )

        started = time.time()
        try:
            result = await self.provider.plan(task)
            self._finish_provider_subagent(subagent_run.id, result)
            return result
        except Exception as exc:
            result = ExecutionResult(
                provider=INTERNAL_EXECUTION_PROVIDER,
                phase="prepare",
                command=["navi", "internal", "--error", "prepare", task.id],
                stdout="",
                stderr=repr(exc),
                exit_code=1,
                started_at=started,
                ended_at=time.time(),
                protocol=ExecutionProtocol.internal_status(
                    run_id=task.id,
                    phase="prepare",
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
