from __future__ import annotations

import asyncio
import codecs
import hashlib
import logging
import shutil
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .capabilities import CapabilityRegistry
from .cron import next_cron_time
from .event_bus import ActionApprovedEvent, ApprovalResolvedEvent, EventBus, AgentTurnCompletedEvent
from .evolution import EvolutionEngine
from .execution import ExecutionService
from .governance_agent import GovernanceAgent
from .graph import GraphNode, GraphStore
from .lifecycle import (
    RUN_STATUS_AWAITING_APPROVAL,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_PENDING,
    RUN_STATUS_PREPARING,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
)
from .runs import Run, RunStore
from .safeguards import redact_secrets
from .text_utils import truncate_middle

logger = logging.getLogger("navi.daemon")

DEFAULT_DEV_PORTS = [3000, 5000, 8000, 8080]
DEFAULT_PORT_PROBE_TIMEOUT_SECONDS = 1.0
MAX_PROJECT_EVENT_CONCURRENCY = 4
MAX_GIT_STATUS_PROMPT_CHARS = 5000
LOG_ERROR_KEYWORDS = ("exception", "fatal", "traceback (most recent call last):")
MAX_LOG_READ_BYTES = 512_000
MAX_LOG_PROMPT_CHARS = 100_000
MAX_FAILED_WATCH_RUN_RECORDS = 50


@dataclass(frozen=True)
class ProactiveEvent:
    source: str
    message: str
    facts: dict[str, Any]
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

    def __init__(self, home: Path, *, project_dir: Path):
        self.home = home
        self.project_dir = project_dir.resolve()
        self.runs = RunStore(home)
        self.execution = ExecutionService(home)
        self.evolution = EvolutionEngine(home)
        self.capabilities = CapabilityRegistry(home=home, project_dir=self.project_dir)
        self.graph = GraphStore(home)
        self.event_bus = EventBus()
        self.governance = GovernanceAgent(home, self.event_bus)

        self._setup_execution_subscription()

    def start(self) -> None:
        from .scheduler import SchedulerStore, SchedulerRunner

        self.scheduler_store = SchedulerStore(self.home)
        self.scheduler_runner = SchedulerRunner(self.scheduler_store, self.event_bus)
        self.scheduler_runner.start()

    def _setup_execution_subscription(self) -> None:
        async def on_action_approved(event: ActionApprovedEvent) -> None:
            task = self.runs.get(event.run_id)
            if task and task.status in {
                RUN_STATUS_AWAITING_APPROVAL,
                RUN_STATUS_QUEUED,
                RUN_STATUS_PREPARING,
            }:
                self.runs.update_run(event.run_id, status=RUN_STATUS_QUEUED)

        async def on_approval_resolved(event: ApprovalResolvedEvent) -> None:
            task = self.runs.get(event.run_id)
            if task and event.decision == "approved":
                self.runs.update_run(event.run_id, status=RUN_STATUS_QUEUED)

        async def on_turn_completed(event: AgentTurnCompletedEvent) -> None:
            if event.session_id:
                try:
                    from .config import load_config
                    from .runtime import AgentRuntime
                    from .provider import build_provider

                    config = load_config(self.home)
                    provider = build_provider(config.model)
                    runtime = AgentRuntime(home=self.home, provider=provider)

                    await runtime.memory.extract_and_consolidate_memories(
                        session_id=event.session_id,
                        provider=provider,
                        run_id=event.run_id,
                    )

                    from navi.goals import GOAL_STATUS_BLOCKED, GoalStore

                    goal_store = GoalStore(self.home)
                    goals = goal_store.list(status="active")
                    for g in goals:
                        if g.session_id != event.session_id:
                            continue
                        # Principle 17: enforce the goal's declared stop condition
                        # before doing more work on it, so a long-running goal is
                        # not kept active past its timeout / retry budget.
                        stop_facts = goal_store.stop_condition_facts(g.id)
                        stop_reason = str(stop_facts.get("reason") or "")
                        if stop_reason:
                            goal_store.update_status(
                                g.id,
                                GOAL_STATUS_BLOCKED,
                                blocked_reason=stop_reason,
                                evidence=stop_facts,
                                event_type="goal.stop_condition",
                            )
                            continue
                        await goal_store.compact_events(g.id, provider)

                    await self.evolution.extract_evals_from_session(
                        event.session_id,
                        run_id=event.run_id,
                    )
                except Exception as e:
                    logger.error(f"Background turn completed task failed: {e}", exc_info=True)

        self.event_bus.subscribe("action_approved", on_action_approved)
        self.event_bus.subscribe("approval_resolved", on_approval_resolved)
        self.event_bus.subscribe("agent_turn_completed", on_turn_completed)

    async def process_queue_once(self) -> list[Run]:
        from .event_bus import RunCompletedEvent

        completed = await self.execution.process_pending_once()
        for task in completed:
            await self.evolution.reflect_run(task, success=task.status == RUN_STATUS_COMPLETED)
            if task.status in (RUN_STATUS_COMPLETED, RUN_STATUS_FAILED):
                await self.event_bus.publish(
                    RunCompletedEvent(
                        run_id=task.id,
                        status=task.status,
                        error=task.error,
                        peer_id=task.peer_id,
                        sender_id=task.sender_id,
                    )
                )
        return completed

    async def process_watches_once(self) -> list[dict]:
        now = time.time()
        created: list[dict] = []

        # 1. Run proactive event-driven checks
        events = await self.process_events_once()
        created.extend(events)

        self._prune_failed_watch_delegate_spawns()

        # 2. Run static cron watches
        for watch in self.runs.due_watches(now):
            result = await self.execution.run_watch(
                prompt=watch.prompt,
                source="watch",
                peer_id=watch.peer_id,
                sender_id=watch.sender_id,
                workspace=watch.workspace,
            )
            created.append(
                {
                    "message": result.summary,
                    "run_id": "",
                    "action": "watch",
                    "observation": result.summary,
                    "peer_id": watch.peer_id,
                    "sender_id": watch.sender_id,
                    "watch_id": watch.id,
                    "ok": result.exit_code == 0,
                }
            )
            if watch.kind == "once":
                self.runs.mark_watch_completed_once(watch.id, last_run_at=now)
            else:
                self.runs.mark_watch_run(
                    watch.id, last_run_at=now, next_run_at=next_cron_time(watch.cron, now=now)
                )
        return created

    def _prune_failed_watch_delegate_spawns(
        self, *, keep_latest: int = MAX_FAILED_WATCH_RUN_RECORDS
    ) -> int:
        keep_latest = max(0, keep_latest)
        total = self.runs.count_runs(
            status=RUN_STATUS_FAILED, source="watch", kind="delegation"
        )
        excess = max(0, total - keep_latest)
        if excess == 0:
            return 0
        stale = self.runs.list_by_status_filtered(
            RUN_STATUS_FAILED, source="watch", kind="delegation", limit=excess
        )
        pruned = 0
        from navi.trace import TraceStore
        trace = TraceStore(self.home)
        for task in stale:
            removed = self.runs.delete_run(task.id)
            if removed is None:
                continue
            self.graph.delete(removed.id)
            trace.add_event(
                trace_id=trace.new_trace_id(),
                phase="daemon.cleanup",
                run_id=removed.id,
                output_data={
                    "action": "pruned_failed_watch_task",
                    "title": removed.title,
                    "workspace": removed.workspace,
                },
            )
            pruned += 1
        return pruned

    async def process_events_once(self) -> list[dict]:
        created: list[dict] = []
        projects = self.graph.list("Project")
        if not projects:
            return created

        active_workspaces = self._active_workspaces()
        primary_project = self._primary_project_name(projects)

        sem = asyncio.Semaphore(MAX_PROJECT_EVENT_CONCURRENCY)

        async def process_project(project: GraphNode) -> list[dict]:
            async with sem:
                return await self._process_project_events(
                    project,
                    active_workspaces=active_workspaces,
                    use_default_ports=project.name == primary_project,
                )

        results = await asyncio.gather(
            *(process_project(project) for project in projects),
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
        project_path = self._resolve_project_path(project.name)
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
            return_exceptions=True,
        )

        for event_batch in event_batches:
            if isinstance(event_batch, Exception):
                logger.warning(
                    "Error running proactive event detector for %s: %s",
                    project_path,
                    event_batch,
                    exc_info=(type(event_batch), event_batch, event_batch.__traceback__),
                )
                continue
            events, state_updates = event_batch
            if state_updates:
                project_data = self._apply_state_updates(project_data, state_updates)
                data_changed = True
            for event in events:
                created_event, event_changed, project_data = await self._apply_event_policy(
                    event,
                    project_data,
                    has_active_task=context.has_active_task,
                    workspace=project_path,
                )
                data_changed = data_changed or event_changed
                if created_event:
                    created.append(created_event)

        if data_changed:
            await asyncio.to_thread(self.graph.upsert, "Project", project.name, project_data)
            self._record_project_graph_mutation(project.name, project_data)
        return created

    def _record_project_graph_mutation(self, project_name: str, project_data: dict[str, Any]) -> str:
        # FP-5/L10: background daemon mutations to the project graph are
        # otherwise untraceable. Record a lightweight trace event so the
        # audit trail covers daemon-initiated state changes.
        from navi.trace import TraceStore

        trace = TraceStore(self.home)
        trace_id = trace.new_trace_id()
        trace.add_event(
            trace_id=trace_id,
            phase="daemon.mutation",
            run_id="",
            output_data={
                "action": "project_graph_upsert",
                "project": project_name,
                "fields": sorted(project_data.keys()),
            },
        )
        trace.evaluate_trace(trace_id)
        return trace_id

    def _project_event_detectors(self) -> tuple[EventDetector, ...]:
        return (
            self._detect_git_mutations,
            self._detect_service_log_events,
            self._detect_port_events,
        )

    def _primary_project_name(self, projects: list[GraphNode]) -> str:
        primary_projects = [project for project in projects if project.data.get("primary")]
        if primary_projects:
            return sorted(primary_projects, key=lambda project: project.name)[0].name

        cwd = self._canonical_path(str(self.project_dir))
        for project in sorted(projects, key=lambda project: project.name):
            if self._canonical_path(project.name) == cwd:
                return project.name

        project_names = sorted(project.name for project in projects)
        return project_names[0] if project_names else ""

    def _active_workspaces(self) -> set[str]:
        return {
            self._canonical_path(task.workspace)
            for task in self.runs.list_by_statuses(
                [RUN_STATUS_RUNNING, RUN_STATUS_QUEUED, RUN_STATUS_PENDING]
            )
            if task.workspace
        }

    @staticmethod
    def _canonical_path(path: str) -> str:
        try:
            return str(Path(path).resolve())
        except OSError:
            return path

    @staticmethod
    def _resolve_project_path(path: str) -> str:
        if not path:
            return ""
        try:
            return str(Path(path).expanduser().resolve(strict=False))
        except OSError:
            return path

    @staticmethod
    def _apply_state_updates(
        target: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        """Return a new dict with ``updates`` merged onto ``target``.

        Immutable merge: callers rebind their own reference to the result rather
        than relying on a hidden in-place mutation of ``target``.
        """
        if not updates:
            return target
        return {**target, **updates}

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
        if shutil.which("git") is None:
            logger.warning("Skipping git proactive detector because git is not on PATH")
            return events, {}

        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "status",
                "--porcelain",
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

            status_text = truncate_middle(status_text, MAX_GIT_STATUS_PROMPT_CHARS)
            events.append(
                ProactiveEvent(
                    source="event_git",
                    message=f"Git filesystem mutation detected in {project_path}.",
                    facts={
                        "kind": "git_status_changed",
                        "project_path": project_path,
                        "changed_files": status_text.splitlines(),
                    },
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

                    events.append(
                        ProactiveEvent(
                            source="event_log",
                            message=f"Exception detected in log {log_rel_path}.",
                            facts={
                                "kind": "log_error_detected",
                                "project_path": project_path,
                                "log_path": log_rel_path,
                                "new_entries": new_content,
                                "matched_error_lines": error_lines,
                            },
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
    def _read_log_diff(
        file_path: Path, last_size: int, read_end: int
    ) -> tuple[str, list[str], int]:
        chunks: list[str] = []
        error_lines: list[str] = []
        total_chars = 0
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending_text = ""
        with open(file_path, "rb") as f:
            f.seek(last_size)
            while f.tell() < read_end:
                line_bytes = f.readline(64_000)
                if not line_bytes:
                    break
                pending_text += decoder.decode(line_bytes, final=False)
                lines = pending_text.splitlines(keepends=True)
                pending_text = ""
                if lines and not lines[-1].endswith(("\n", "\r")):
                    pending_text = lines.pop()
                for line in lines:
                    total_chars = SystemDaemon._append_log_prompt_chunk(chunks, total_chars, line)
                    if any(keyword in line.lower() for keyword in LOG_ERROR_KEYWORDS):
                        error_lines.append(redact_secrets(line.strip()))
            new_offset = f.tell()
        pending_text += decoder.decode(b"", final=True)
        if pending_text:
            total_chars = SystemDaemon._append_log_prompt_chunk(chunks, total_chars, pending_text)
            if any(keyword in pending_text.lower() for keyword in LOG_ERROR_KEYWORDS):
                error_lines.append(redact_secrets(pending_text.strip()))
        return "".join(chunks), error_lines, new_offset

    @staticmethod
    def _append_log_prompt_chunk(chunks: list[str], total_chars: int, line: str) -> int:
        # Principle 13/16: external log content is untrusted and may contain
        # secrets; redact before it enters the prompt-bound diff or error facts.
        if total_chars < MAX_LOG_PROMPT_CHARS:
            chunks.append(redact_secrets(line))
            total_chars += len(line)
        return total_chars

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

        async def probe_port_family(port: int, family: socket.AddressFamily) -> bool:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection("localhost", port, family=family),
                    timeout=self._port_probe_timeout(project_data),
                )
                writer.close()
                await writer.wait_closed()
                return True
            except OSError:
                return False
            except asyncio.TimeoutError:
                return False

        async def probe_port(port: int) -> tuple[int, bool]:
            probe_results = await asyncio.gather(
                probe_port_family(port, socket.AF_INET),
                probe_port_family(port, socket.AF_INET6),
            )
            return port, any(probe_results)

        probe_results = await asyncio.gather(*(probe_port(port) for port in normalized_ports))
        for port, is_active in probe_results:
            port_key = f"port_active_{port}"
            was_active = project_data.get(port_key, False)
            if was_active and not is_active:
                events.append(
                    ProactiveEvent(
                        source="event_port",
                        message=f"Local service on port {port} went offline.",
                        facts={
                            "kind": "port_went_offline",
                            "port": port,
                            "previous_active": was_active,
                            "active": is_active,
                        },
                        state_updates={port_key: is_active},
                        suppressed_state_updates={port_key: is_active},
                    )
                )
                continue

            if is_active != was_active:
                state_updates[port_key] = is_active
        return events, state_updates

    @staticmethod
    def _port_probe_timeout(project_data: dict[str, Any]) -> float:
        raw = project_data.get("port_probe_timeout_seconds", DEFAULT_PORT_PROBE_TIMEOUT_SECONDS)
        try:
            return max(0.5, min(float(raw), 10.0))
        except (TypeError, ValueError):
            return DEFAULT_PORT_PROBE_TIMEOUT_SECONDS

    async def _apply_event_policy(
        self,
        event: ProactiveEvent,
        project_data: dict,
        *,
        has_active_task: bool,
        workspace: str,
    ) -> tuple[dict | None, bool, dict]:
        policy_updates = event.suppressed_state_updates if has_active_task else event.state_updates
        changed = bool(policy_updates)
        project_data = self._apply_state_updates(project_data, policy_updates)
        if has_active_task:
            return None, changed, project_data

        return (
            self._event_result(event, workspace=workspace),
            changed,
            project_data,
        )

    @staticmethod
    def _event_result(event: ProactiveEvent, *, workspace: str) -> dict:
        return {
            "message": event.message,
            "facts": event.facts,
            "run_id": "",
            "action": "runtime.observation",
            "observation": event.message,
            "workspace": workspace,
        }
