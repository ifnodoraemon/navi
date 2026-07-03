"""Execution provider: NaviExecutionProvider and engine registry."""

from navi.lifecycle import Phase, Governance, Acceptance, Resolution
import asyncio
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..capabilities import CapabilityRegistry
from ..provider import ChatMessage, ModelPool
from ..runs import Run
from ..safeguards import redact_secrets
from ..tools import ACTUATOR_CONTEXT
from ..prompting import PromptLayerStore
from .protocol import (
    INTERNAL_EXECUTION_PROVIDER,
    SUBAGENT_NOTIFICATION_ROLE,
    SUBAGENT_PLANNER_ROLE,
    ExecutionProtocol,
    ExecutionResult,
    execution_output_schema,
    execution_protocol_instruction,
    require_workspace_value,
    task_workspace,
)

_engine_class: type | None = None


def register_engine_class(cls: type) -> None:
    global _engine_class
    _engine_class = cls


def get_engine_class() -> type:
    if _engine_class is None:
        raise RuntimeError("HernessEngine class has not been registered yet.")
    return _engine_class


class NaviExecutionProvider:
    """Internal execution planner backed by Navi's configured model pool."""

    def __init__(self, *, provider: ModelPool, timeout_seconds: float, home: Path):
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.home = home

    async def plan(self, task: Run) -> ExecutionResult:
        return await self._complete_task(
            task,
            phase=Phase.RUNNING,
            role=SUBAGENT_PLANNER_ROLE,
            messages=self._prepare_messages(task),
        )

    async def run_watch(
        self, *, prompt: str, source: str, peer_id: str, sender_id: str, workspace: str
    ) -> ExecutionResult:
        workspace = require_workspace_value(workspace)
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
                stderr=redact_secrets(str(exc)),
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
                    output_schema=execution_output_schema(phase),
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
                stderr=redact_secrets(str(exc)),
                exit_code=1,
                started_at=started,
                ended_at=time.time(),
                model_role=role,
            )

    def _prepare_messages(self, task: Run) -> list[ChatMessage]:
        registry = CapabilityRegistry(
            home=self.home,
            project_dir=task_workspace(task),
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
                    + execution_protocol_instruction("prepare")
                    + f"\n\nCapability Manifest:\n{manifest}"
                ),
            ),
            ChatMessage(
                "user",
                (
                    f"Run id: {task.id}\n"
                    f"Workspace: {task_workspace(task)}\n"
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
                reason_code="execution_protocol_invalid",
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
            phase=Phase.RUNNING,
            status="completed" if ok else "failed",
            summary=summary or "scheduled watch completed",
            reason_code="watch_notification_completed" if ok else "watch_notification_failed",
            action_kind="watch_notification",
        )
        return ExecutionResult(
            provider=INTERNAL_EXECUTION_PROVIDER,
            phase=Phase.RUNNING,
            command=["navi", "subagent", SUBAGENT_NOTIFICATION_ROLE, "watch"],
            stdout=stdout,
            stderr="" if ok else stderr,
            exit_code=0 if ok else exit_code,
            started_at=started_at,
            ended_at=ended_at,
            protocol=protocol,
            model_role=SUBAGENT_NOTIFICATION_ROLE,
        )
