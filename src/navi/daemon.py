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
from .graph import GraphStore
from .tasks import Task, TaskStore

logger = logging.getLogger("navi.daemon")


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
                except Exception:
                    active_workspaces.add(t.workspace)

        for project in projects:
            project_path = project.name
            if not project_path or not Path(project_path).exists():
                continue

            # Use a local mutable copy of project data to avoid cross-section overwrites
            project_data = dict(project.data)
            data_changed = False

            # Check if there's an active task in progress for this project
            try:
                project_canonical_str = str(Path(project_path).resolve())
            except Exception:
                project_canonical_str = project_path
            has_active_task = project_canonical_str in active_workspaces

            # A. Check Git status mutations
            git_dir = Path(project_path) / ".git"
            if git_dir.exists():
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
                        logger.warning(f"Git status timed out for {project_path}")
                        stdout = b""
                    status_text = stdout.decode(errors="replace").strip()
                    if status_text:
                        current_hash = hashlib.sha256(status_text.encode()).hexdigest()
                        last_hash = project_data.get("last_git_status_hash", "")
                        
                        if current_hash != last_hash:
                            # Only update state and trigger task if there's no active task
                            if not has_active_task:
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
                                created.append({
                                    "message": f"Git filesystem mutation detected in {project_path}.",
                                    "task_id": result.task_id,
                                    "action": result.action,
                                    "observation": result.observation,
                                })
                            # If has_active_task, do NOT update hash so we re-detect next tick
                except (OSError, asyncio.SubprocessError) as e:
                    logger.warning(f"Error checking git status for {project_path}: {e}")

            # B. Check local service logs for errors/exceptions
            log_dirs = [Path(project_path), Path(project_path) / "logs", Path(project_path) / "log"]
            for log_dir in log_dirs:
                if not log_dir.exists():
                    continue
                for file_path in log_dir.glob("*.log"):
                    try:
                        file_stat = file_path.stat()
                        current_size = file_stat.st_size
                        log_key = f"log_size_{file_path.name}"
                        last_size = project_data.get(log_key, 0)

                        if current_size < last_size:
                            last_size = 0
                            project_data[log_key] = 0
                            data_changed = True

                        if current_size > last_size:
                            # Cap maximum bytes read per tick to prevent OOM on huge log deltas
                            max_read_bytes = 512_000
                            read_end = min(last_size + max_read_bytes, current_size)

                            def read_log_diff():
                                error_lines = []
                                full_content_accumulator = []
                                total_chars = 0
                                with open(file_path, "rb") as f:
                                    f.seek(last_size)
                                    bytes_remaining = read_end - last_size
                                    for line_bytes in f:
                                        bytes_remaining -= len(line_bytes)
                                        line = line_bytes.decode("utf-8", errors="replace")
                                        if total_chars < 100000:
                                            full_content_accumulator.append(line)
                                            total_chars += len(line)
                                        if any(word in line for word in ["Exception", "FATAL", "Traceback (most recent call last):"]):
                                            error_lines.append(line.strip())
                                        if bytes_remaining <= 0:
                                            break
                                    new_offset = f.tell()
                                return "".join(full_content_accumulator), error_lines, new_offset

                            new_content, error_lines, new_last_size = await asyncio.to_thread(read_log_diff)
                                
                            if error_lines:
                                error_fingerprint = hashlib.sha256("\n".join(error_lines).encode()).hexdigest()
                                last_fingerprint = project_data.get(f"last_err_fp_{file_path.name}", "")
                                
                                if error_fingerprint != last_fingerprint:
                                    if not has_active_task:
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
                                        created.append({
                                            "message": f"Exception detected in log {file_path.name}.",
                                            "task_id": result.task_id,
                                            "action": result.action,
                                            "observation": result.observation,
                                        })
                                        
                                        project_data[log_key] = new_last_size
                                        project_data[f"last_err_fp_{file_path.name}"] = error_fingerprint
                                        data_changed = True
                                    # If has_active_task, do NOT advance log pointer so we re-read next tick
                                else:
                                    # Fingerprint matched (same error), just advance the log pointer
                                    project_data[log_key] = new_last_size
                                    data_changed = True
                            else:
                                # No errors found, safely advance the log pointer
                                project_data[log_key] = new_last_size
                                data_changed = True
                    except OSError as e:
                        logger.warning(f"Error reading log file {file_path}: {e}")
                          
            # C. Check socket exceptions / connection status for active development ports
            # Probing default ports only for the first/primary project, or if explicitly declared in `dev_ports`
            dev_ports = project_data.get("dev_ports", [])
            if not dev_ports and project == projects[0]:
                dev_ports = [3000, 5000, 8000, 8080]
            
            if dev_ports:
                async def probe_port(port: int) -> tuple[int, bool]:
                    try:
                        _, writer = await asyncio.wait_for(
                            asyncio.open_connection("localhost", port),
                            timeout=0.5
                        )
                        writer.close()
                        await writer.wait_closed()
                        return port, True
                    except Exception:
                        return port, False

                probe_results = await asyncio.gather(*(probe_port(p) for p in dev_ports))
                for port, is_active in probe_results:
                    port_key = f"port_active_{port}"
                    was_active = project_data.get(port_key, False)
                    
                    if was_active and not is_active:
                        if not has_active_task:
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
                            created.append({
                                "message": f"Local service on port {port} went offline.",
                                "task_id": result.task_id,
                                "action": result.action,
                                "observation": result.observation,
                            })
                        
                    if is_active != was_active:
                        project_data[port_key] = is_active
                        data_changed = True

            # Single upsert at the end of the project loop to avoid cross-section overwrites
            if data_changed:
                self.graph.upsert("Project", project_path, project_data)
                        
        return created

