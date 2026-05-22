from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .capabilities import CapabilityContext, CapabilityRegistry
from .cron import next_cron_time
from .evolution import EvolutionEngine
from .execution import ExecutionService
from .graph import GraphNode, GraphStore
from .tasks import Task, TaskStore

logger = logging.getLogger("navi.daemon")

DEFAULT_DEV_PORTS = [3000, 5000, 8000, 8080]
LOG_ERROR_KEYWORDS = ("exception", "fatal", "traceback (most recent call last):")
MAX_LOG_READ_BYTES = 512_000
MAX_LOG_PROMPT_CHARS = 100_000


@dataclass(frozen=True)
class ProactiveEvent:
    source: str
    message: str
    prompt: str
    state_updates: dict[str, Any] = field(default_factory=dict)
    suppressed_state_updates: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectEventContext:
    project_path: str
    project_data: dict[str, Any]
    has_active_task: bool
    use_default_ports: bool


EventBatch = tuple[list[ProactiveEvent], dict[str, Any]]
EventDetector = Callable[[ProjectEventContext], Awaitable[EventBatch]]


class SystemDaemon:
    """Background OS primitives, not user-intent workflow."""

    def __init__(self, home: Path):
        self.home = home
        self.tasks = TaskStore(home)
        self.execution = ExecutionService(home)
        self.evolution = EvolutionEngine(home)
        self.capabilities = CapabilityRegistry(home=home, project_dir=Path.cwd())
        self.graph = GraphStore(home)

    async def process_queue_once(self) -> list[Task]:
        completed = await self.execution.process_pending_once()
        for task in completed:
            await self.evolution.reflect_task(task, success=task.status == "completed")
        return completed

    async def process_watches_once(self) -> list[dict]:
        now = time.time()
        created: list[dict] = []
        
        # 1. Run proactive event-driven checks
        events = await self.process_events_once()
        created.extend(events)
        
        # 2. Run static cron watches
        for watch in self.tasks.due_watches(now):
            result = await self.capabilities.invoke(
                "task.create",
                {"prompt": watch.prompt},
                permission="prepare",
                context=CapabilityContext(
                    home=self.home,
                    peer_id=watch.peer_id,
                    sender_id=watch.sender_id,
                    source="watch",
                ),
            )
            created.append(
                {
                    "message": result.message or result.observation,
                    "task_id": result.task_id,
                    "action": result.action,
                    "observation": result.observation,
                }
            )
            self.tasks.mark_watch_run(watch.id, last_run_at=now, next_run_at=next_cron_time(watch.cron, now=now))
        return created

    async def process_events_once(self) -> list[dict]:
        created: list[dict] = []
        projects = self.graph.list("Project")
        if not projects:
            return created
        
        active_workspaces = self._active_workspaces()
        primary_project = projects[0].name if projects else ""
        results = await asyncio.gather(
            *[
                self._process_project_events(
                    project,
                    active_workspaces=active_workspaces,
                    use_default_ports=project.name == primary_project,
                )
                for project in projects
            ],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.warning(
                    "Error processing proactive project events: %s",
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )
                continue
            created.extend(result)
        return created

    async def _process_project_events(
        self,
        project: GraphNode,
        *,
        active_workspaces: set[str],
        use_default_ports: bool,
    ) -> list[dict]:
        created: list[dict] = []
        project_path = project.name
        if not project_path or not Path(project_path).exists():
            return created

        project_data = dict(project.data)
        data_changed = False
        context = ProjectEventContext(
            project_path=project_path,
            project_data=project_data,
            has_active_task=self._canonical_path(project_path) in active_workspaces,
            use_default_ports=use_default_ports,
        )

        event_batches = await asyncio.gather(
            *(detector(context) for detector in self._project_event_detectors()),
        )

        for events, state_updates in event_batches:
            data_changed = self._apply_state_updates(project_data, state_updates) or data_changed
            for event in events:
                created_event, event_changed = await self._apply_event_policy(
                    event,
                    project_data,
                    has_active_task=context.has_active_task,
                )
                data_changed = data_changed or event_changed
                if created_event:
                    created.append(created_event)

        if data_changed:
            self.graph.upsert("Project", project_path, project_data)
        return created

    def _project_event_detectors(self) -> tuple[EventDetector, ...]:
        return (
            self._detect_git_mutations,
            self._detect_service_log_events,
            self._detect_port_events,
        )

    def _active_workspaces(self) -> set[str]:
        return {
            self._canonical_path(task.workspace)
            for task in self.tasks.list_by_statuses(["running", "queued", "pending"])
            if task.workspace
        }

    @staticmethod
    def _canonical_path(path: str) -> str:
        try:
            return str(Path(path).resolve())
        except OSError:
            return path

    @staticmethod
    def _apply_state_updates(target: dict[str, Any], updates: dict[str, Any]) -> bool:
        target.update(updates)
        return bool(updates)

    async def _detect_git_mutations(
        self,
        context: ProjectEventContext,
    ) -> EventBatch:
        events: list[ProactiveEvent] = []
        project_path = context.project_path
        project_data = context.project_data
        git_dir = Path(project_path) / ".git"
        if not git_dir.exists():
            return events, {}

        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain",
                cwd=project_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.warning("Git status timed out for %s", project_path)
                return events, {}
            status_text = stdout.decode(errors="replace").strip()
            if not status_text:
                return events, {}

            current_hash = hashlib.sha256(status_text.encode()).hexdigest()
            last_hash = project_data.get("last_git_status_hash", "")
            if current_hash == last_hash:
                return events, {}

            prompt = (
                f"A filesystem modification was detected in the project {project_path}.\n"
                f"Modified files:\n{status_text}\n"
                f"Evaluate the changes, run tests if applicable, and verify code correctness."
            )
            events.append(
                ProactiveEvent(
                    source="event_git",
                    message=f"Git filesystem mutation detected in {project_path}.",
                    prompt=prompt,
                    state_updates={"last_git_status_hash": current_hash},
                    suppressed_state_updates={"last_git_status_hash": current_hash},
                )
            )
        except (OSError, asyncio.SubprocessError) as e:
            logger.warning("Error checking git status for %s: %s", project_path, e)
        return events, {}

    async def _detect_service_log_events(
        self,
        context: ProjectEventContext,
    ) -> EventBatch:
        events: list[ProactiveEvent] = []
        state_updates: dict[str, Any] = {}
        project_path = context.project_path
        project_data = context.project_data
        log_dirs = [Path(project_path), Path(project_path) / "logs", Path(project_path) / "log"]
        for log_dir in log_dirs:
            if not log_dir.exists():
                continue
            for file_path in log_dir.glob("*.log"):
                try:
                    log_rel_path = str(file_path.relative_to(project_path))
                    current_size = file_path.stat().st_size
                    log_key = f"log_size_{log_rel_path}"
                    last_size = project_data.get(log_key, 0)
                    if current_size < last_size:
                        last_size = 0
                        state_updates[log_key] = 0

                    if current_size <= last_size:
                        continue

                    read_end = min(last_size + MAX_LOG_READ_BYTES, current_size)
                    new_content, error_lines, new_last_size = await asyncio.to_thread(
                        self._read_log_diff,
                        file_path,
                        last_size,
                        read_end,
                    )
                    if not error_lines:
                        state_updates[log_key] = new_last_size
                        continue

                    error_fingerprint = hashlib.sha256("\n".join(error_lines).encode()).hexdigest()
                    fp_key = f"last_err_fp_{log_rel_path}"
                    last_fingerprint = project_data.get(fp_key, "")
                    if error_fingerprint == last_fingerprint:
                        state_updates[log_key] = new_last_size
                        continue

                    prompt = (
                        f"Proactive Alert: I detected an exception/error in local service log file: {log_rel_path}\n"
                        f"New log entries:\n{new_content}\n"
                        f"Analyze the error, find the root cause, and propose a fix."
                    )
                    events.append(
                        ProactiveEvent(
                            source="event_log",
                            message=f"Exception detected in log {log_rel_path}.",
                            prompt=prompt,
                            state_updates={
                                log_key: new_last_size,
                                fp_key: error_fingerprint,
                            },
                            suppressed_state_updates={
                                log_key: new_last_size,
                                fp_key: error_fingerprint,
                            },
                        )
                    )
                except OSError as e:
                    logger.warning("Error reading log file %s: %s", file_path, e)
        return events, state_updates

    @staticmethod
    def _read_log_diff(file_path: Path, last_size: int, read_end: int) -> tuple[str, list[str], int]:
        chunks: list[str] = []
        error_lines: list[str] = []
        total_chars = 0
        with open(file_path, "rb") as f:
            f.seek(last_size)
            while f.tell() < read_end:
                remaining = read_end - f.tell()
                line_bytes = f.readline(min(remaining, 64_000))
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace")
                if total_chars < MAX_LOG_PROMPT_CHARS:
                    chunks.append(line)
                    total_chars += len(line)
                if any(keyword in line.lower() for keyword in LOG_ERROR_KEYWORDS):
                    error_lines.append(line.strip())
            new_offset = f.tell()
        return "".join(chunks), error_lines, new_offset

    async def _detect_port_events(
        self,
        context: ProjectEventContext,
    ) -> EventBatch:
        events: list[ProactiveEvent] = []
        state_updates: dict[str, Any] = {}
        project_data = context.project_data
        dev_ports = project_data.get("dev_ports", [])
        if not dev_ports and context.use_default_ports:
            dev_ports = DEFAULT_DEV_PORTS
        if not dev_ports:
            return events, state_updates

        normalized_ports: list[int] = []
        for port in dev_ports:
            try:
                normalized_ports.append(int(port))
            except (TypeError, ValueError):
                logger.warning("Ignoring invalid dev port value: %r", port)

        async def probe_port(port: int) -> tuple[int, bool]:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection("localhost", port),
                    timeout=0.5,
                )
                writer.close()
                await writer.wait_closed()
                return port, True
            except OSError:
                return port, False
            except asyncio.TimeoutError:
                return port, False

        probe_results = await asyncio.gather(*(probe_port(port) for port in normalized_ports))
        for port, is_active in probe_results:
            port_key = f"port_active_{port}"
            was_active = project_data.get(port_key, False)
            if was_active and not is_active:
                prompt = (
                    f"Proactive Alert: The local service on port {port} has stopped responding or crashed.\n"
                    f"Please inspect the running processes, verify the server status, and restart the service if needed."
                )
                events.append(
                    ProactiveEvent(
                        source="event_port",
                        message=f"Local service on port {port} went offline.",
                        prompt=prompt,
                        state_updates={port_key: is_active},
                        suppressed_state_updates={port_key: is_active},
                    )
                )
                continue

            if is_active != was_active:
                state_updates[port_key] = is_active
        return events, state_updates

    async def _apply_event_policy(
        self,
        event: ProactiveEvent,
        project_data: dict,
        *,
        has_active_task: bool,
    ) -> tuple[dict | None, bool]:
        policy_updates = event.suppressed_state_updates if has_active_task else event.state_updates
        if has_active_task:
            return None, self._apply_state_updates(project_data, policy_updates)

        result = await self.capabilities.invoke(
            "task.create",
            {"prompt": event.prompt},
            permission="prepare",
            context=CapabilityContext(
                home=self.home,
                peer_id="daemon",
                sender_id="daemon",
                source=event.source,
            ),
        )
        data_changed = self._apply_state_updates(project_data, policy_updates)
        return (
            self._event_result(event, result),
            data_changed,
        )

    @staticmethod
    def _event_result(event: ProactiveEvent, result: Any) -> dict:
        return {
            "message": event.message,
            "task_id": result.task_id,
            "action": result.action,
            "observation": result.observation,
        }
