from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path

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
        
        # Query all active (running/queued/pending) tasks to avoid loop triggers on projects with active tasks
        running_tasks = self.tasks.list_by_status("running")
        queued_tasks = self.tasks.list_by_status("queued")
        pending_tasks = self.tasks.list_by_status("pending")
        active_tasks = running_tasks + queued_tasks + pending_tasks

        # Pre-build a set of resolved workspaces for O(1) active task lookup
        active_workspaces: set[str] = set()
        for t in active_tasks:
            if t.workspace:
                try:
                    active_workspaces.add(str(Path(t.workspace).resolve()))
                except OSError:
                    active_workspaces.add(t.workspace)

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

        try:
            project_canonical_str = str(Path(project_path).resolve())
        except OSError:
            project_canonical_str = project_path
        has_active_task = project_canonical_str in active_workspaces

        git_created, git_changed = await self._check_git_mutations(
            project_path,
            project_data,
            has_active_task=has_active_task,
        )
        created.extend(git_created)
        data_changed = data_changed or git_changed

        log_created, log_changed = await self._check_service_logs(
            project_path,
            project_data,
            has_active_task=has_active_task,
        )
        created.extend(log_created)
        data_changed = data_changed or log_changed

        port_created, port_changed = await self._probe_dev_ports(
            project_data,
            has_active_task=has_active_task,
            use_default_ports=use_default_ports,
        )
        created.extend(port_created)
        data_changed = data_changed or port_changed

        if data_changed:
            self.graph.upsert("Project", project_path, project_data)
        return created

    async def _check_git_mutations(
        self,
        project_path: str,
        project_data: dict,
        *,
        has_active_task: bool,
    ) -> tuple[list[dict], bool]:
        created: list[dict] = []
        data_changed = False
        git_dir = Path(project_path) / ".git"
        if not git_dir.exists():
            return created, data_changed

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
                return created, data_changed
            status_text = stdout.decode(errors="replace").strip()
            if not status_text:
                return created, data_changed

            current_hash = hashlib.sha256(status_text.encode()).hexdigest()
            last_hash = project_data.get("last_git_status_hash", "")
            if current_hash == last_hash:
                return created, data_changed

            if has_active_task:
                return created, data_changed

            project_data["last_git_status_hash"] = current_hash
            data_changed = True
            prompt = (
                f"A filesystem modification was detected in the project {project_path}.\n"
                f"Modified files:\n{status_text}\n"
                f"Evaluate the changes, run tests if applicable, and verify code correctness."
            )
            result = await self.capabilities.invoke(
                "task.create",
                {"prompt": prompt},
                permission="prepare",
                context=CapabilityContext(
                    home=self.home,
                    peer_id="daemon",
                    sender_id="daemon",
                    source="event_git",
                ),
            )
            created.append(
                {
                    "message": f"Git filesystem mutation detected in {project_path}.",
                    "task_id": result.task_id,
                    "action": result.action,
                    "observation": result.observation,
                }
            )
        except (OSError, asyncio.SubprocessError) as e:
            logger.warning("Error checking git status for %s: %s", project_path, e)
        return created, data_changed

    async def _check_service_logs(
        self,
        project_path: str,
        project_data: dict,
        *,
        has_active_task: bool,
    ) -> tuple[list[dict], bool]:
        created: list[dict] = []
        data_changed = False
        log_dirs = [Path(project_path), Path(project_path) / "logs", Path(project_path) / "log"]
        for log_dir in log_dirs:
            if not log_dir.exists():
                continue
            for file_path in log_dir.glob("*.log"):
                try:
                    current_size = file_path.stat().st_size
                    log_key = f"log_size_{file_path.name}"
                    last_size = project_data.get(log_key, 0)
                    if current_size < last_size:
                        last_size = 0
                        project_data[log_key] = 0
                        data_changed = True

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
                        project_data[log_key] = new_last_size
                        data_changed = True
                        continue

                    error_fingerprint = hashlib.sha256("\n".join(error_lines).encode()).hexdigest()
                    last_fingerprint = project_data.get(f"last_err_fp_{file_path.name}", "")
                    if error_fingerprint == last_fingerprint:
                        project_data[log_key] = new_last_size
                        data_changed = True
                        continue

                    if has_active_task:
                        continue

                    prompt = (
                        f"Proactive Alert: I detected an exception/error in local service log file: {file_path.name}\n"
                        f"New log entries:\n{new_content}\n"
                        f"Analyze the error, find the root cause, and propose a fix."
                    )
                    result = await self.capabilities.invoke(
                        "task.create",
                        {"prompt": prompt},
                        permission="prepare",
                        context=CapabilityContext(
                            home=self.home,
                            peer_id="daemon",
                            sender_id="daemon",
                            source="event_log",
                        ),
                    )
                    created.append(
                        {
                            "message": f"Exception detected in log {file_path.name}.",
                            "task_id": result.task_id,
                            "action": result.action,
                            "observation": result.observation,
                        }
                    )
                    project_data[log_key] = new_last_size
                    project_data[f"last_err_fp_{file_path.name}"] = error_fingerprint
                    data_changed = True
                except OSError as e:
                    logger.warning("Error reading log file %s: %s", file_path, e)
        return created, data_changed

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

    async def _probe_dev_ports(
        self,
        project_data: dict,
        *,
        has_active_task: bool,
        use_default_ports: bool,
    ) -> tuple[list[dict], bool]:
        created: list[dict] = []
        data_changed = False
        dev_ports = project_data.get("dev_ports", [])
        if not dev_ports and use_default_ports:
            dev_ports = DEFAULT_DEV_PORTS
        if not dev_ports:
            return created, data_changed

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
            if was_active and not is_active and not has_active_task:
                prompt = (
                    f"Proactive Alert: The local service on port {port} has stopped responding or crashed.\n"
                    f"Please inspect the running processes, verify the server status, and restart the service if needed."
                )
                result = await self.capabilities.invoke(
                    "task.create",
                    {"prompt": prompt},
                    permission="prepare",
                    context=CapabilityContext(
                        home=self.home,
                        peer_id="daemon",
                        sender_id="daemon",
                        source="event_port",
                    ),
                )
                created.append(
                    {
                        "message": f"Local service on port {port} went offline.",
                        "task_id": result.task_id,
                        "action": result.action,
                        "observation": result.observation,
                    }
                )

            if is_active != was_active:
                project_data[port_key] = is_active
                data_changed = True
        return created, data_changed
