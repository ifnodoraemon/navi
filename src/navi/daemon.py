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
from subprocess import SubprocessError
from typing import Any, Awaitable, Callable

from .capabilities import CapabilityRegistry
from .event_bus import (
    ActionApprovedEvent,
    AgentTurnCompletedEvent,
    ApprovalResolvedEvent,
    EventBus,
    NaviEvent,
)
from .evolution import EvolutionEngine
from .governance_agent import GovernanceAgent
from .graph import GraphNode, GraphStore
from .lifecycle import Governance, Phase, Resolution
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
        self.event_bus = EventBus()
        self.evolution = EvolutionEngine(home)
        self.capabilities = CapabilityRegistry(home=home, project_dir=self.project_dir)
        self.graph = GraphStore(home)
        self.governance = GovernanceAgent(home, self.event_bus)

        self._setup_execution_subscription()

    def start(self) -> None:
        """Start background scheduling primitives.

        Safe to call from synchronous entry points (CLI, API factory): if no
        event loop is running we simply skip the scheduler-start (the
        scheduler will be started lazily when the loop is up, or the daemon
        will be driven manually via :meth:`process_queue_once`)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.scheduler_runner = None
            return
        try:
            from .scheduler import SchedulerStore, SchedulerRunner

            self.scheduler_store = SchedulerStore(self.home)
            self.scheduler_runner = SchedulerRunner(self.scheduler_store, self.event_bus)
            self.scheduler_runner.start()
        except RuntimeError:
            # No running event loop - scheduler will not auto-advance.
            # Callers that drive the daemon manually (process_queue_once) are unaffected.
            self.scheduler_runner = None

    def _setup_execution_subscription(self) -> None:
        async def on_action_approved(event: NaviEvent) -> None:
            assert isinstance(event, ActionApprovedEvent)
            task = self.runs.get(event.run_id)
            if task and task.phase == Phase.PENDING:
                self.runs.update_run(event.run_id, phase=Phase.PENDING, result_summary="")

        async def on_approval_resolved(event: NaviEvent) -> None:
            assert isinstance(event, ApprovalResolvedEvent)
            task = self.runs.get(event.run_id)
            if task and event.decision == "approved":
                self.runs.update_run(
                    event.run_id,
                    phase=Phase.PENDING,
                    governance=Governance.APPROVED,
                    resolution=Resolution.NONE,
                    result_summary="",
                )

        async def on_turn_completed(event: NaviEvent) -> None:
            assert isinstance(event, AgentTurnCompletedEvent)
            if event.session_id:
                try:
                    from .config import load_config
                    from .runtime import AgentRuntime
                    from .provider import build_provider

                    config = load_config(self.home)
                    provider = build_provider(config.model)
                    runtime = AgentRuntime(home=self.home, provider=provider)

                    from navi.lifecycle import Phase, Resolution
                    from navi.goals import GoalStore

                    goal_store = GoalStore(self.home)
                    goals = [g for g in goal_store.list() if g.phase in (Phase.PENDING, Phase.RUNNING, Phase.PAUSED)]
                    for g in goals:
                        if g.session_id != event.session_id:
                            continue
                        # Principle 17: enforce the goal's declared stop condition
                        # before doing more work on it, so a long-running goal is
                        # not kept active past its timeout / retry budget.
                        stop_facts = goal_store.stop_condition_facts(g.id)
                        stop_reason = str(stop_facts.get("reason") or "")
                        if stop_reason:
                            goal_store.update_state(
                                g.id,
                                phase=Phase.ENDED,
                                resolution=Resolution.BLOCKED,
                                blocked_reason=stop_reason,
                                evidence=stop_facts,
                                event_type="goal.stop_condition",
                            )
                            continue
                        await goal_store.compact_events(g.id, runtime)

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
        from .config import load_config
        from .provider import build_provider
        from .runtime import AgentRuntime
        from .loop_runs import LoopRunStore
        from .loop_control_service import LoopControlService
        from .goal_state_graph import run_goal_loop_state_graph
        from .capabilities import CapabilityRegistry
        from .capabilities_types import CapabilityContext
        from .goals import GoalStore

        loop_runs = LoopRunStore(self.home)
        active_states = loop_runs.list_active(limit=10)
        if not active_states:
            return []

        config = load_config(self.home)
        provider = build_provider(config.model)
        runtime = AgentRuntime(home=self.home, provider=provider)
        service = LoopControlService(self.home)
        goals = GoalStore(self.home)

        affected_runs = []
        for state in active_states:
            goal = goals.get(state.goal_id)
            if not goal:
                continue

            try:
                prepared = service.resume_loop(loop_run_id=state.run_id, workspace=goal.workspace)
                
                permission_ceiling = prepared.loop_spec.goal.permission_ceiling
                planner_capabilities = CapabilityRegistry(
                    home=self.home,
                    project_dir=Path(goal.workspace),
                    permission_ceiling=permission_ceiling,
                    enforce_connector_source_policy=False,
                    runtime=runtime,
                )
                context = CapabilityContext(
                    home=self.home,
                    source=goal.source,
                    peer_id=goal.peer_id,
                    sender_id=goal.sender_id,
                    session_id=goal.session_id,
                    permission_ceiling=permission_ceiling,
                    workspace=goal.workspace,
                    enforce_connector_source_policy=False,
                )
                
                result = await run_goal_loop_state_graph(
                    home=self.home,
                    service=service,
                    base=prepared,
                    runtime=runtime,
                    planner_capabilities=planner_capabilities,
                    context=context,
                    evidence={"entrypoint": "daemon.process_queue_once", "resumed": True},
                    result_evidence={"state_graph_mode": "llm_backed", "resumed": True},
                    state_transition="resumed",
                )
                affected_runs.append(result.run)
            except Exception as e:
                logger.error(f"Failed to process loop run {state.run_id}: {e}", exc_info=True)

        return affected_runs

    async def process_watches_once(self) -> list[dict]:
        created: list[dict] = []

        await self.process_memory_maintenance_once()

        # 1. Run proactive event-driven checks
        events = await self.process_events_once()
        created.extend(events)


        # 2. Materialize due recurring templates as ordinary child goals.
        from .cron import next_cron_time

        now = time.time()
        from .goals import GoalStore
        from .loop_control_service import LoopControlService

        goal_store = GoalStore(self.home)
        service = LoopControlService(self.home)
        
        due_goals = goal_store.due_cron_goals(now)
        for g in due_goals:
            try:
                occurrence = service.open_scheduled_occurrence(g)

                next_time = next_cron_time(g.cron_schedule, now=now)
                goal_store.update_cron_run(g.id, next_time)
                created.append(
                    {
                        "cron_goal_id": g.id,
                        "goal_id": occurrence.goal.id,
                        "run_id": occurrence.run.id,
                        "peer_id": g.peer_id,
                        "triggered": True,
                        "next_run_at": next_time,
                        "surface": False,
                    }
                )
            except Exception as e:
                logger.error("Failed to process cron goal %s: %s", g.id, e, exc_info=True)

        return created

    async def process_memory_maintenance_once(self) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self._process_memory_maintenance)
        except Exception as exc:
            logger.error("Background memory maintenance failed: %s", exc, exc_info=True)
            return {"ok": False, "error": str(exc)}

    def _process_memory_maintenance(self) -> dict[str, Any]:
        from .memory import MemoryStore

        memory = MemoryStore(self.home)
        gc_facts = memory.garbage_collect()
        graph_facts = memory.sync_semantic_graph(graph_store=self.graph)
        return {
            "ok": True,
            "memory_gc": gc_facts,
            "semantic_graph": graph_facts,
        }


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
            if isinstance(result, BaseException):
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
            if isinstance(event_batch, BaseException):
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
        from datetime import datetime

        trace = TraceStore(self.home)
        # Use a single daily trace ID to prevent polluting the trace database with thousands of traces
        daily_suffix = datetime.now().strftime("%Y-%m-%d")
        trace_id = f"daemon-trace-{daily_suffix}"
        
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
        # Evaluate trace is optional here, but we can keep it
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
            for task in self.runs.list_by_phases(
                [Phase.RUNNING, Phase.PENDING, Phase.PAUSED]
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
        except (OSError, SubprocessError) as e:
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
            "action": "runtime.fact",
            "observation": event.message,
            "workspace": workspace,
        }
