from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from .capabilities import CapabilityRegistry
from .daemon_types import (
    MAX_PROJECT_EVENT_CONCURRENCY,
    EventDetector,
    ProjectEventContext,
    ProactiveEvent,
)
from .detectors import GitMutationDetector, PortEventDetector, ServiceLogDetector
from .event_bus import (
    ActionApprovedEvent,
    AgentTurnCompletedEvent,
    EventBus,
    NaviEvent,
)
from .governance_agent import GovernanceAgent
from .graph import GraphNode, GraphStore
from .lifecycle import Phase
from .runs import Run, RunStore

logger = logging.getLogger("navi.daemon")


class SystemDaemon:
    """Background OS primitives, not user-intent workflow."""

    def __init__(self, home: Path, *, project_dir: Path):
        self.home = home
        self.project_dir = project_dir.resolve()
        self.runs = RunStore(home)
        self.event_bus = EventBus()
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
                    goals = [
                        g
                        for g in goal_store.list()
                        if g.phase in (Phase.PENDING, Phase.RUNNING, Phase.PAUSED)
                    ]
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
                except Exception as e:
                    logger.error(f"Background turn completed task failed: {e}", exc_info=True)

        self.event_bus.subscribe("action_approved", on_action_approved)
        self.event_bus.subscribe("agent_turn_completed", on_turn_completed)

    async def process_queue_once(self) -> list[Run]:
        from .config import load_config
        from .provider import build_provider
        from .runtime import AgentRuntime
        from .loop_runs import LoopRunStore
        from .goal_state_graph import resume_goal_loop_run
        from .goals import GoalStore

        loop_runs = LoopRunStore(self.home)
        # Active is a lifecycle fact, not permission for the daemon to execute.
        # Foreground turns and manually prepared goals have their own explicit
        # owner; only loops created for background execution belong here.
        active_states = loop_runs.list_active_for_execution_mode("background", limit=10)
        if not active_states:
            return []

        config = load_config(self.home)
        provider = build_provider(config.model)
        runtime = AgentRuntime(home=self.home, provider=provider)
        goals = GoalStore(self.home)

        affected_runs = []
        for state in active_states:
            goal = goals.get(state.goal_id)
            if not goal:
                continue

            try:
                result = await resume_goal_loop_run(
                    home=self.home,
                    loop_run_id=state.run_id,
                    runtime=runtime,
                    event_bus=self.event_bus,
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

        sem = asyncio.Semaphore(MAX_PROJECT_EVENT_CONCURRENCY)

        async def process_project(project: GraphNode) -> list[dict]:
            async with sem:
                return await self._process_project_events(
                    project,
                    active_workspaces=active_workspaces,
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

    def _record_project_graph_mutation(
        self, project_name: str, project_data: dict[str, Any]
    ) -> str:
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
            GitMutationDetector(),
            ServiceLogDetector(),
            PortEventDetector(),
        )

    def _active_workspaces(self) -> set[str]:
        return {
            self._canonical_path(task.workspace)
            for task in self.runs.list_by_phases([Phase.RUNNING, Phase.PENDING, Phase.PAUSED])
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
    def _apply_state_updates(target: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        """Return a new dict with ``updates`` merged onto ``target``.

        Immutable merge: callers rebind their own reference to the result rather
        than relying on a hidden in-place mutation of ``target``.
        """
        if not updates:
            return target
        return {**target, **updates}

    async def _apply_event_policy(
        self,
        event: ProactiveEvent,
        project_data: dict,
        *,
        has_active_task: bool,
        workspace: str,
    ) -> tuple[dict | None, bool, dict]:
        if has_active_task:
            return None, False, project_data

        changed = bool(event.state_updates)
        project_data = self._apply_state_updates(project_data, event.state_updates)

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
