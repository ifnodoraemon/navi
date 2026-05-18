from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path

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


class CodexCliProvider:
    name = "codex"

    async def plan(self, task: Task) -> ExecutionResult:
        if self._mock_enabled():
            return self._mock(task, "plan", "Plan: inspect the request, estimate risk, then request approval.")
        return await self._run(task, phase="plan", sandbox="read-only")

    async def execute(self, task: Task) -> ExecutionResult:
        if self._mock_enabled():
            return self._mock(task, "execute", f"Executed task: {task.prompt}")
        return await self._run(task, phase="execute", sandbox="workspace-write")

    async def _run(self, task: Task, *, phase: str, sandbox: str) -> ExecutionResult:
        prompt = self._prompt(task, phase=phase)
        command = [
            "codex",
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
        stdout_raw, stderr_raw = await proc.communicate()
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
        command = ["codex", "exec", "--mock", phase, task.id]
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
        if phase == "plan":
            return (
                "You are an execution specialist called by Navi. "
                "Do not modify files. Produce a concise plan, risks, expected files, and whether approval is needed.\n\n"
                f"Task: {task.prompt}"
            )
        return (
            "You are an execution specialist called by Navi. "
            "Execute the approved task. Do not bypass sandboxing or approvals. "
            "End with a concise summary of changes and verification.\n\n"
            f"Task: {task.prompt}"
        )

    @staticmethod
    def _mock_enabled() -> bool:
        return os.environ.get("NAVI_CODEX_MOCK", "").lower() in {"1", "true", "yes"}


class ExecutionService:
    def __init__(self, home: Path):
        self.tasks = TaskStore(home)
        self.codex = CodexCliProvider()

    async def plan_task(self, task: Task) -> Task:
        self.tasks.update_task(task.id, status="planning")
        result = await self.codex.plan(task)
        self._log(task, result)
        status = "planned" if result.exit_code == 0 else "failed"
        return self.tasks.update_task(
            task.id,
            status=status,
            plan_summary=result.summary,
            error="" if result.exit_code == 0 else result.stderr,
        ) or task

    async def execute_task(self, task: Task) -> Task:
        self.tasks.update_task(task.id, status="running")
        result = await self.codex.execute(task)
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
