from __future__ import annotations

import time
from pathlib import Path

from .capabilities import CapabilityContext, CapabilityRegistry
from .cron import next_cron_time
from .evolution import EvolutionEngine
from .execution import ExecutionService
from .tasks import Task, TaskStore


class SystemDaemon:
    """Background OS primitives, not user-intent workflow."""

    def __init__(self, home: Path):
        self.home = home
        self.tasks = TaskStore(home)
        self.execution = ExecutionService(home)
        self.evolution = EvolutionEngine(home)
        self.capabilities = CapabilityRegistry(home=home, project_dir=Path.cwd())

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
        import asyncio
        import hashlib
        import socket
        from .graph import GraphStore
        
        created: list[dict] = []
        graph = GraphStore(self.home)
        projects = graph.list("Project")
        
        for project in projects:
            project_path = project.name
            if not project_path or not Path(project_path).exists():
                continue
                
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
                    stdout, _ = await proc.communicate()
                    status_text = stdout.decode().strip()
                    if status_text:
                        current_hash = hashlib.sha256(status_text.encode()).hexdigest()
                        last_hash = project.data.get("last_git_status_hash", "")
                        
                        if current_hash != last_hash:
                            # Update git status hash in project node data
                            updated_data = {**project.data, "last_git_status_hash": current_hash}
                            graph.upsert("Project", project_path, updated_data)
                            
                            # Trigger a proactive task/alert
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
                except Exception:
                    pass

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
                        last_size = project.data.get(log_key, 0)
                        
                        if current_size > last_size:
                            def read_log_diff():
                                with open(file_path, "r", errors="replace") as f:
                                    f.seek(last_size)
                                    return f.readlines()
                            new_lines = await asyncio.to_thread(read_log_diff)
                                
                            new_content = "".join(new_lines)
                            if any(word in new_content for word in ["Exception", "FATAL", "Traceback (most recent call first)"]):
                                prompt = (
                                    f"Proactive Alert: I detected an exception/error in local service log file: {file_path.name}\n"
                                    f"New log entries:\n{new_content[-1500:]}\n"
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
                            
                            updated_data = {**project.data, log_key: current_size}
                            graph.upsert("Project", project_path, updated_data)
                    except Exception:
                         pass
                         
            # C. Check socket exceptions / connection status for active development ports
            dev_ports = [3000, 5000, 8000, 8080]
            for port in dev_ports:
                port_key = f"port_active_{port}"
                was_active = project.data.get(port_key, False)
                
                is_active = False
                try:
                    _, writer = await asyncio.wait_for(
                        asyncio.open_connection("localhost", port),
                        timeout=0.2
                    )
                    writer.close()
                    await writer.wait_closed()
                    is_active = True
                except Exception:
                    pass
                    
                if was_active and not is_active:
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
                    updated_data = {**project.data, port_key: is_active}
                    graph.upsert("Project", project_path, updated_data)
                    
        return created
