from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .cli_providers import list_cli_provider_specs
from .config import ExecutionConfig, load_config
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
        self.config = load_config(home).execution
        self.tasks = TaskStore(home)
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
        self.tasks.update_task(task.id, status="running")
        result = await self._provider_call_with_timeout(task, "execute")
        self._log(task, result)
        status = "completed" if result.exit_code == 0 else "failed"
        return self.tasks.update_task(
            task.id,
            status=status,
            result_summary=result.summary,
            error="" if result.exit_code == 0 else result.stderr,
        ) or task

    async def process_pending_once(self, *, limit: int = 3) -> list[Task]:
        completed: list[Task] = []
        for task in self.tasks.list_by_status("queued", limit=limit):
            completed.append(await self.execute_task(task))
        return completed

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
