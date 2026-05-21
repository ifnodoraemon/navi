from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .cli_providers import list_cli_provider_specs
from .config import ExecutionConfig, load_config
from .governance import GovernanceEngine
from .tasks import Task, TaskStore


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

    @property
    def summary(self) -> str:
        text = (self.stdout or self.stderr).strip()
        return text[:1600] if text else f"{self.phase} exited with {self.exit_code}"


class CliExecutionProvider:
    def __init__(self, *, name: str, binary: str, config: ExecutionConfig):
        self.name = name
        self.binary = binary
        self.config = config

    async def plan(self, task: Task) -> ExecutionResult:
        if self._mock_enabled():
            return self._mock(task, "prepare", "Preparation: inspect the request, estimate risk, then request approval.")
        return await self._run(task, phase="prepare", sandbox="read-only")

    async def execute(self, task: Task) -> ExecutionResult:
        if self._mock_enabled():
            return self._mock(task, "execute", f"Executed task: {task.prompt}")
        return await self._run(task, phase="execute", sandbox="workspace-write")

    async def _run(self, task: Task, *, phase: str, sandbox: str) -> ExecutionResult:
        prompt = self._prompt(task, phase=phase)
        command = [
            self.binary,
            "exec",
            "-C",
            task.workspace or str(Path.home()),
            "--sandbox",
            sandbox,
            "--json",
            prompt,
        ]
        started = time.time()
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_seconds())
        except asyncio.TimeoutError:
            proc.kill()
            stdout_raw, stderr_raw = await proc.communicate()
            ended = time.time()
            return ExecutionResult(
                provider=self.name,
                phase=phase,
                command=command,
                stdout=stdout_raw.decode(errors="replace"),
                stderr=f"{self.name} {phase} timed out after {self._timeout_seconds()} seconds",
                exit_code=124,
                started_at=started,
                ended_at=ended,
            )
        ended = time.time()
        return ExecutionResult(
            provider=self.name,
            phase=phase,
            command=command,
            stdout=stdout_raw.decode(errors="replace"),
            stderr=stderr_raw.decode(errors="replace"),
            exit_code=proc.returncode or 0,
            started_at=started,
            ended_at=ended,
        )

    def _mock(self, task: Task, phase: str, text: str) -> ExecutionResult:
        now = time.time()
        command = [self.binary, "exec", "--mock", phase, task.id]
        return ExecutionResult(
            provider=self.name,
            phase=phase,
            command=command,
            stdout=text,
            stderr="",
            exit_code=0,
            started_at=now,
            ended_at=now,
        )

    @staticmethod
    def _prompt(task: Task, *, phase: str) -> str:
        if phase == "prepare":
            return (
                "You are an execution specialist called by Navi. "
                "Do not modify files. Produce concise preparation facts: risk, expected actions, touched areas, and whether approval is needed.\n\n"
                f"Task: {task.prompt}"
            )
        return (
            "You are an execution specialist called by Navi. "
            "Execute the approved task. Do not bypass sandboxing or approvals. "
            "End with a concise summary of changes and verification.\n\n"
            f"Task: {task.prompt}"
        )

    def _mock_enabled(self) -> bool:
        return self.config.mock

    def _timeout_seconds(self) -> float:
        raw = os.environ.get("NAVI_EXECUTION_TIMEOUT_SECONDS", str(self.config.timeout_seconds))
        try:
            return max(1.0, float(raw))
        except ValueError:
            return self.config.timeout_seconds


class ExecutionService:
    def __init__(self, home: Path):
        self.home = home
        self.config = load_config(home).execution
        self.tasks = TaskStore(home)
        self.governance = GovernanceEngine(home)
        self.providers = {
            spec.name: CliExecutionProvider(name=spec.name, binary=spec.binary, config=self.config)
            for spec in list_cli_provider_specs()
            if spec.supports_execution
        }

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
        import json
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
        
        # Self-healing retry loop
        retries = 0
        max_retries = 2
        accumulated_history = ""
        while result.exit_code != 0 and retries < max_retries:
            retries += 1
            
            attempt_log = (
                f"=== SELF-HEALING ATTEMPT {retries} ===\n"
                f"Your previous attempt to execute the task failed with exit code {result.exit_code}.\n"
                f"Command: {' '.join(result.command)}\n"
                f"Stdout:\n{result.stdout}\n"
                f"Stderr:\n{result.stderr}\n\n"
            )
            accumulated_history += attempt_log
            
            healing_prompt = (
                f"{accumulated_history}"
                f"Please analyze the errors, stack traces, and failures. "
                f"Formulate a fix (e.g. editing files, fixing imports, or adjusting configurations), apply it, and verify that the task runs successfully."
            )
            
            healing_task = Task(
                id=task.id,
                title=task.title,
                prompt=f"{task.prompt}\n\n{healing_prompt}",
                kind=task.kind,
                source=task.source,
                peer_id=task.peer_id,
                sender_id=task.sender_id,
                provider=task.provider,
                workspace=task.workspace,
                autonomy_level=task.autonomy_level,
                trust_rule_id=task.trust_rule_id,
                status="running",
                plan_summary=task.plan_summary,
                result_summary=task.result_summary,
                error=task.error,
                why_now=task.why_now,
                created_at=task.created_at,
                updated_at=task.updated_at,
            )
            
            result = await self._provider_call_with_timeout(healing_task, "execute")
            self._log(task, result)
            
        status = "completed" if result.exit_code == 0 else "failed"
        updated_task = self.tasks.update_task(
            task.id,
            status=status,
            result_summary=result.summary,
            error="" if result.exit_code == 0 else result.stderr,
        ) or task
        
        # Record the evolution event
        task_after = self.tasks.get(task.id)
        after_state = json.dumps(
            {
                "status": task_after.status if task_after else status,
                "result_summary": task_after.result_summary if task_after else result.summary,
                "error": task_after.error if task_after else ("" if result.exit_code == 0 else result.stderr),
            },
            sort_keys=True
        )
        
        from .evolution import EvolutionLedger
        ledger = EvolutionLedger(self.home)
        ledger.record(
            task_id=task.id,
            target_type="task_execution",
            target_id=task.id,
            reason=f"task execution {'completed' if result.exit_code == 0 else 'failed'} (retries: {retries})",
            before=before_state,
            after=after_state,
        )
        
        return updated_task


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

    def _execution_allowed(self, task: Task) -> bool:
        return self.governance.execution_allowed(task)

    def _log(self, task: Task, result: ExecutionResult) -> None:
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

    async def _provider_call_with_timeout(self, task: Task, phase: str) -> ExecutionResult:
        started = time.time()
        provider = self.providers.get(task.provider or self.config.provider) or next(iter(self.providers.values()))
        timeout = provider._timeout_seconds()
        try:
            if phase == "prepare":
                return await asyncio.wait_for(provider.plan(task), timeout=timeout + 1)
            return await asyncio.wait_for(provider.execute(task), timeout=timeout + 1)
        except asyncio.TimeoutError:
            ended = time.time()
            return ExecutionResult(
                provider=provider.name,
                phase=phase,
                command=[provider.binary, "exec", "--timeout", phase, task.id],
                stdout="",
                stderr=f"{provider.name} {phase} timed out after {timeout} seconds",
                exit_code=124,
                started_at=started,
                ended_at=ended,
            )
