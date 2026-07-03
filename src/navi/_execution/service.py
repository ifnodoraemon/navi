"""Execution service: orchestrates runs, governance, and the provider."""

from __future__ import annotations
from navi.lifecycle import Phase, Governance, Acceptance, Resolution

import json
import time
from pathlib import Path
from typing import Any

from ..capability_contract import (
    CAPABILITY_REASON_KEY,
    CAPABILITY_REASON_SENSITIVE_APPROVAL,
)
from ..config import load_config
from ..governance import GovernanceEngine
from ..evolution import EvolutionLedger
from ..goals import GoalStore
from ..lifecycle import (
    execute_finalize_decision,
    execution_ledger_reason,
    prepare_run_status,
)
from ..provider import build_provider
from ..runs import Run, RunStore
from ..subagents import (
    SUBAGENT_STATUS_COMPLETED,
    SUBAGENT_STATUS_FAILED,
    SUBAGENT_STATUS_SUSPENDED,
    SubagentRunStore,
)
from .protocol import (
    INTERNAL_EXECUTION_PROVIDER,
    SUBAGENT_EXECUTOR_ROLE,
    SUBAGENT_NOTIFICATION_ROLE,
    SUBAGENT_PLANNER_ROLE,
    ExecutionProtocol,
    ExecutionResult,
    require_workspace_value,
    task_workspace,
)
from .provider import NaviExecutionProvider
from . import provider as _provider


class ExecutionService:
    def __init__(self, home: Path, *, event_bus=None):
        self.home = home
        config = load_config(home)
        self.config = config.execution
        self.runs = RunStore(home)
        self.governance = GovernanceEngine(home)
        self.ledger = EvolutionLedger(home)
        self.subagents = SubagentRunStore(home)
        self.event_bus = event_bus
        self.provider = NaviExecutionProvider(
            provider=build_provider(config.model),
            timeout_seconds=self.config.timeout_seconds,
            home=home,
        )

    def recover_stale_runs(self) -> None:
        """Mark runs stuck in transient states as failed due to system restart."""
        stale_statuses = ()
        for status in stale_statuses:
            for run in self.runs.list_by_phases([status], limit=100):
                self.runs.update_run(run.id, phase=Phase.ENDED, resolution=Resolution.FAILED, error="Run interrupted by system restart.")
                self.runs.reject_pending_approvals_for_run(run.id)

    async def plan_task(self, task: Run) -> Run:
        self.runs.update_run(task.id, phase=Phase.PENDING)
        result = await self._prepare_with_subagent(task)
        self._log(task, result)
        task_after_prepare = self.runs.get(task.id)
        status = prepare_run_status(
            exit_code=result.exit_code,
            current_status=task_after_prepare.phase if task_after_prepare else "",
        )
        if status == "running":
            phase = "running"
            res = "none"
        else:
            phase = "ended"
            res = "failed"
            
        updated = self.runs.update_run(
            task.id,
            phase=phase, governance=Governance.NONE, acceptance=Acceptance.NONE, resolution=res,
            plan_summary=result.summary,
            error="" if result.exit_code == 0 else result.stderr,
        ) or task
        GoalStore(self.home).update_for_run(
            updated,
            evidence={"run_id": updated.id, "run_status": updated.phase, "phase": "running"},
        )
        return updated

    async def execute_task(self, task: Run) -> Run:
        before_state = self._execution_before_state(task)

        permission_ceiling = "write"
        self.runs.update_run(task.id, phase=Phase.RUNNING)

        from ..runtime import AgentRuntime

        HernessEngine = _provider.get_engine_class()

        config = load_config(self.home)
        runtime = AgentRuntime(home=self.home, provider=build_provider(config.model))

        started_at = time.time()
        subagent_run = self.subagents.start(
            role=SUBAGENT_EXECUTOR_ROLE,
            phase=Phase.RUNNING,
            run_id=task.id,
            command=["navi", "subagent", SUBAGENT_EXECUTOR_ROLE, "execute", task.id],
            input_data={"workspace": task.workspace, "autonomy_level": task.autonomy_level},
        )

        session_alias = f"executor:{task.id}"

        from ..memory import MemoryStore

        memory = MemoryStore(self.home)
        existing_alias = memory.get_session_alias(session_alias)
        has_history = False
        if existing_alias:
            messages = memory.get_messages(existing_alias.session_id, limit=50)
            has_history = len(messages) > 0

        if has_history:
            prompt_text = ""
        else:
            prompt_text = task.prompt

        if task.kind == "delegation":
            disabled_classes = frozenset({"delegation", "approval", "conversation"})
        else:
            disabled_classes = frozenset({"delegation", "approval"})

        engine = HernessEngine(
            home=self.home,
            runtime=runtime,
            project_dir=task_workspace(task),
            permission_ceiling=permission_ceiling,
            disabled_capability_classes=disabled_classes,
            enforce_connector_source_policy=False,
            governed_run_id=task.id,
        )
        turn_result = await engine.handle(
            prompt_text,
            peer_id=task.peer_id,
            sender_id=task.sender_id,
            source=task.source,
            session_alias=session_alias,
        )

        if getattr(turn_result, "yields_control", False):
            self.runs.update_run(
                task.id,
                phase=Phase.PAUSED, result_summary=turn_result.text,
            )
            self.subagents.finish(
                subagent_run.id,
                status=SUBAGENT_STATUS_SUSPENDED,
                output_data={"exit_code": 0, "summary": turn_result.text},
                error="",
            )
            if self.event_bus and task.kind != "delegation":
                import asyncio

                from ..event_bus import RunSuspendedEvent

                asyncio.create_task(
                    self.event_bus.publish(
                        RunSuspendedEvent(
                            run_id=task.id,
                            text=turn_result.text,
                            peer_id=task.peer_id,
                            sender_id=task.sender_id,
                            source=task.source,
                        )
                    )
                )
            return self.runs.get(task.id)

        phase, gov, res, status_reason = self._execution_status_from_turn_result(turn_result)
        exit_code = 0 if phase != "ended" or res != "failed" else 1
        self.subagents.finish(
            subagent_run.id,
            status=SUBAGENT_STATUS_FAILED if exit_code else SUBAGENT_STATUS_COMPLETED,
            output_data={
                "exit_code": exit_code,
                "execution_status": f"{phase}:{gov}:{res}",
                "summary": turn_result.text,
                "provider": "react",
                "model_role": turn_result.model_role,
                "trace_id": turn_result.trace_id,
            },
            error=turn_result.text if exit_code != 0 else "",
        )
        # A sensitive op inside the run may have suspended it for a fresh
        # approval. That state is intentional — return it as-is so the finalizer
        # does not overwrite awaiting_approval with completed/failed. The daemon
        # surfaces the new code to the user via the normal background channel.
        suspended = self.runs.get(task.id)
        if suspended is not None and suspended.phase == "paused" and suspended.governance == "awaiting_approval":
            self.subagents.finish(
                subagent_run.id,
                status=SUBAGENT_STATUS_SUSPENDED,
                output_data={"exit_code": 0, "summary": suspended.result_summary},
                error="",
            )
            GoalStore(self.home).update_for_run(
                suspended,
                evidence={"run_id": suspended.id, "run_status": suspended.status},
            )
            return suspended
        protocol = ExecutionProtocol.internal_status(
            run_id=task.id,
            phase=Phase.RUNNING,
            status=execution_status,
            summary=turn_result.text[:1600] if turn_result.text else "",
            reason_code=status_reason,
            action_kind="herness_engine",
        )
        result = ExecutionResult(
            provider="react",
            phase=Phase.RUNNING,
            command=["navi", "react", task.id],
            stdout=turn_result.text,
            stderr=status_reason if exit_code != 0 else (turn_result.text if exit_code != 0 else ""),
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

    @staticmethod
    def _execution_status_from_turn_result(result) -> tuple[str, str, str, str]:
        facts = result.facts if isinstance(result.facts, dict) else {}
        if getattr(result, "yields_control", False):
            return Phase.PAUSED, Governance.NONE, Resolution.NONE, "execution produced an ask action and is waiting for user input"
        if facts.get(CAPABILITY_REASON_KEY) == CAPABILITY_REASON_SENSITIVE_APPROVAL:
            return Phase.PAUSED, Governance.AWAITING_APPROVAL, Resolution.NONE, "execution suspended for approval"
        if not getattr(result, "ok", True):
            return Phase.ENDED, Governance.NONE, Resolution.FAILED, "execution ended with capability error facts"
        return Phase.ENDED, Governance.NONE, Resolution.SUCCESS, "execution produced terminal completion facts"

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
            completion_status=str((result.protocol.completion if result.protocol else {}).get("status") or ""),
        )
        updated_task = (
            self.runs.update_run(
                task.id,
                phase=finalize.phase, resolution=finalize.resolution,
                result_summary=result.summary,
                error=finalize.error,
            )
            or task
        )

        # Record the evolution event
        after_state = json.dumps(
            {
                "phase": updated_task.phase, "resolution": updated_task.resolution,
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
                "run_status": updated_task.phase,
                "phase": "execute",
                "summary": updated_task.result_summary,
            },
        )

        return updated_task

    async def run_watch(
        self, *, prompt: str, source: str, peer_id: str, sender_id: str, workspace: str
    ) -> ExecutionResult:
        workspace = require_workspace_value(workspace)
        watch_task = Run(
            id="",
            title=prompt[:120],
            phase=Phase.RUNNING,
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
            phase=Phase.RUNNING,
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
            status=(
                SUBAGENT_STATUS_COMPLETED
                if result.exit_code == 0
                else SUBAGENT_STATUS_FAILED
            ),
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
        for task in self.runs.list_by_phase(limit=limit):
            if task.result_summary:
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
            phase=Phase.RUNNING,
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
                phase=Phase.RUNNING,
                command=["navi", "internal", "--error", "prepare", task.id],
                stdout="",
                stderr=repr(exc),
                exit_code=1,
                started_at=started,
                ended_at=time.time(),
                protocol=ExecutionProtocol.internal_status(
                    run_id=task.id,
                    phase=Phase.RUNNING,
                    status="failed",
                    summary=repr(exc),
                    reason_code="execution_provider_exception",
                    action_kind="execution_error",
                ),
            )
            self._finish_provider_subagent(subagent_run.id, result)
            return result

    def _finish_provider_subagent(self, subagent_id: str, result: ExecutionResult) -> None:
        self.subagents.finish(
            subagent_id,
            status=(
                SUBAGENT_STATUS_COMPLETED
                if result.exit_code == 0
                else SUBAGENT_STATUS_FAILED
            ),
            output_data={
                "exit_code": result.exit_code,
                "summary": result.summary,
                "provider": result.provider,
                "model_role": result.model_role,
            },
            error=result.stderr,
        )
