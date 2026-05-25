from __future__ import annotations

import asyncio
import json
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


@dataclass(frozen=True)
class ExecutionProtocol:
    version: str = EXECUTION_PROTOCOL_VERSION
    phase: str = ""
    task_id: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    completion: dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        summary = str(self.completion.get("summary") or "").strip()
        return summary[:1600]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "phase": self.phase,
            "task_id": self.task_id,
            "actions": self.actions,
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
        actions = _required_dict_list(payload, "actions")
        evidence = _required_dict_list(payload, "evidence")
        verification = _required_dict(payload, "verification")
        completion = _required_dict(payload, "completion")
        if not str(completion.get("summary") or "").strip():
            raise ValueError("execution protocol completion.summary is required")
        return cls(
            version=version,
            phase=payload_phase,
            task_id=payload_task_id,
            actions=actions,
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
            actions=[
                {
                    "kind": action_kind,
                    "target": task_id,
                    "status": status,
                    "summary": summary,
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


def _execution_protocol_instruction(phase: str) -> str:
    return (
        "Return one JSON object with key `navi_execution`. "
        f"`navi_execution.phase` must be `{phase}` and `version` must be `{EXECUTION_PROTOCOL_VERSION}`. "
        "Include `actions` as concrete attempted actions, `evidence` as observable facts or artifacts, "
        "`verification` with status/checks/reason, and `completion` with status and summary. "
        "If no local mutation was performed, say so in actions and verification."
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
            protocol=ExecutionProtocol.internal_status(
                task_id=task.id,
                phase=phase,
                status="completed",
                summary=text,
                reason="execution mock mode",
                action_kind="mock_execution",
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
        self.provider = NaviExecutionProvider(
            provider=build_provider(config.model),
            timeout_seconds=self.config.timeout_seconds,
        )

    async def plan_task(self, task: Task) -> Task:
        self.tasks.update_task(task.id, status="preparing")
        result = await self._provider_call_with_timeout(task, "prepare")
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
        return await self.provider.run_watch(
            prompt=prompt,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            workspace=workspace,
        )

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
