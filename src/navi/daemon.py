from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .daemon_types import (
    MAX_PROJECT_EVENT_CONCURRENCY,
    EventDetector,
    ProjectEventContext,
    ProactiveEvent,
)
from .detectors import GitMutationDetector, PortEventDetector, ServiceLogDetector
from .event_bus import AgentTurnCompletedEvent, EventBus, NaviEvent
from .graph import GraphNode, GraphStore
from .runs import Run, RunStore

logger = logging.getLogger("navi.daemon")


class SystemDaemon:
    """Background OS primitives, not user-intent workflow."""

    def __init__(self, home: Path, *, project_dir: Path):
        self.home = home
        self.project_dir = project_dir.resolve()
        self.runs = RunStore(home)
        self.event_bus = EventBus()
        self.graph = GraphStore(home)

        self._setup_subscriptions()

    def _setup_subscriptions(self) -> None:
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
                    goals = goal_store.list_scoped(
                        session_id=event.session_id,
                        phases=(Phase.PENDING, Phase.RUNNING, Phase.PAUSED),
                        limit=None,
                    )
                    for g in goals:
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
        execution_owner = f"daemon:{os.getpid()}:{uuid.uuid4().hex}"
        active_states = loop_runs.claim_active_for_execution_mode(
            "background", owner=execution_owner, limit=10
        )
        retryable_candidates = loop_runs.list_retryable_background_pauses(limit=10)
        retryable_states = [
            claimed
            for item in retryable_candidates
            if (
                claimed := loop_runs.claim_for_execution(
                    item.run_id,
                    owner=execution_owner,
                    allow_paused=True,
                )
            )
            is not None
        ]
        states = [
            *active_states,
            *(state for state in retryable_states if state.run_id not in {item.run_id for item in active_states}),
        ]
        retryable_ids = {state.run_id for state in retryable_states}
        if not states:
            return []

        config = load_config(self.home)
        provider = build_provider(config.model)
        runtime = AgentRuntime(home=self.home, provider=provider)
        goals = GoalStore(self.home)

        affected_runs = []
        for state in states:
            goal = goals.get(state.goal_id)
            if not goal:
                continue

            try:
                result = await resume_goal_loop_run(
                    home=self.home,
                    loop_run_id=state.run_id,
                    runtime=runtime,
                    event_bus=self.event_bus,
                    entrypoint="system_daemon.process_queue_once",
                    resume_reason="background_execution",
                    state_transition="background_resumed",
                    resource_retry=state.run_id in retryable_ids,
                    execution_owner=execution_owner,
                )
                affected_runs.append(result.run)
            except Exception as e:
                logger.error(f"Failed to process loop run {state.run_id}: {e}", exc_info=True)

        return affected_runs

    async def process_background_once(self) -> list[dict]:
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
            scheduled_for = g.next_run_at
            trace_id = f"cron-{g.id}-{int(scheduled_for)}"
            try:
                next_time = next_cron_time(g.cron_schedule, now=now)
            except Exception as schedule_error:
                error_type = type(schedule_error).__name__
                error = str(schedule_error)[:1000]
                goal_store.block_invalid_cron_schedule(
                    g.id,
                    scheduled_for=scheduled_for,
                    error_type=error_type,
                    error=error,
                    trace_id=trace_id,
                )
                from .loop import TracePhase
                from .trace import TraceStore

                failure_facts = {
                    "kind": "scheduled_template_invalid",
                    "cron_goal_id": g.id,
                    "objective": g.objective,
                    "cron_schedule": g.cron_schedule,
                    "scheduled_for": scheduled_for,
                    "state_transition": "schedule_blocked",
                    "error_type": error_type,
                    "error": error,
                }
                trace = TraceStore(self.home)
                trace.add_event(
                    trace_id=trace_id,
                    phase=TracePhase.CAPABILITY_RESULT,
                    run_id=g.run_id,
                    source=g.source,
                    peer_id=g.peer_id,
                    sender_id=g.sender_id,
                    tool="goal.schedule",
                    ok=False,
                    input_data={
                        "cron_goal_id": g.id,
                        "scheduled_for": scheduled_for,
                    },
                    output_data=failure_facts,
                    message="Persisted schedule violates the cron contract",
                )
                trace.evaluate_trace(trace_id)
                created.append(
                    {
                        "cron_goal_id": g.id,
                        "peer_id": g.peer_id,
                        "workspace": g.workspace,
                        "trace_id": trace_id,
                        "facts": failure_facts,
                        "surface": True,
                    }
                )
                continue
            try:
                occurrence = service.open_scheduled_occurrence(g)

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
                error_type = type(e).__name__
                error = str(e)[:1000]
                goal_store.record_cron_failure(
                    g.id,
                    scheduled_for=scheduled_for,
                    next_run_at=next_time,
                    error_type=error_type,
                    error=error,
                    trace_id=trace_id,
                )
                from .loop import TracePhase
                from .trace import TraceStore

                failure_facts = {
                    "kind": "scheduled_occurrence_failed",
                    "cron_goal_id": g.id,
                    "objective": g.objective,
                    "cron_schedule": g.cron_schedule,
                    "scheduled_for": scheduled_for,
                    "next_run_at": next_time,
                    "error_type": error_type,
                    "error": error,
                }
                trace = TraceStore(self.home)
                trace.add_event(
                    trace_id=trace_id,
                    phase=TracePhase.CAPABILITY_RESULT,
                    run_id=g.run_id,
                    source=g.source,
                    peer_id=g.peer_id,
                    sender_id=g.sender_id,
                    tool="goal.open_scheduled_occurrence",
                    ok=False,
                    input_data={
                        "cron_goal_id": g.id,
                        "scheduled_for": scheduled_for,
                    },
                    output_data=failure_facts,
                    message="Scheduled occurrence creation failed and advanced",
                )
                trace.evaluate_trace(trace_id)
                created.append(
                    {
                        "cron_goal_id": g.id,
                        "peer_id": g.peer_id,
                        "workspace": g.workspace,
                        "trace_id": trace_id,
                        "facts": failure_facts,
                        "surface": True,
                    }
                )

        return created

    async def process_memory_maintenance_once(self) -> dict[str, Any]:
        try:
            facts = await asyncio.to_thread(self._process_memory_maintenance)
            from .config import load_config
            from .memory import MemoryStore
            from .provider import build_provider
            from .runtime import AgentRuntime

            memory = MemoryStore(self.home)
            owner = f"memory-daemon:{os.getpid()}:{uuid.uuid4().hex}"
            jobs = await asyncio.to_thread(
                memory.claim_consolidation_jobs,
                owner=owner,
                limit=10,
            )
            if not jobs:
                facts["consolidation"] = {"claimed": 0, "completed": 0, "failed": 0}
                from .retention import DataRetentionManager

                facts["retention"] = await asyncio.to_thread(
                    lambda: DataRetentionManager(self.home).compact_expired().to_dict()
                )
                return await self._add_observability_maintenance(facts)

            config = load_config(self.home)
            runtime = AgentRuntime(home=self.home, provider=build_provider(config.model))
            completed = 0
            failures: list[dict[str, str]] = []
            for job in jobs:
                try:
                    await memory.consolidate_job(job, runtime)
                    completed += 1
                except Exception as exc:
                    failures.append(
                        {
                            "job_id": job.id,
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:500],
                        }
                    )
                    logger.warning(
                        "Memory consolidation job %s failed: %s",
                        job.id,
                        exc,
                        exc_info=True,
                    )
            facts["consolidation"] = {
                "claimed": len(jobs),
                "completed": completed,
                "failed": len(failures),
                "failures": failures,
            }
            from .retention import DataRetentionManager

            facts["retention"] = await asyncio.to_thread(
                lambda: DataRetentionManager(self.home).compact_expired().to_dict()
            )
            return await self._add_observability_maintenance(facts)
        except Exception as exc:
            logger.error("Background memory maintenance failed: %s", exc, exc_info=True)
            return {"ok": False, "error": str(exc)}

    async def _add_observability_maintenance(self, facts: dict[str, Any]) -> dict[str, Any]:
        from .metrics import MetricsProjector

        projector = MetricsProjector(self.home)
        facts["evolution_observations"] = await asyncio.to_thread(
            projector.observe_evolution_activations
        )
        snapshot = await asyncio.to_thread(projector.snapshot)
        facts["slo"] = {
            "overall_status": snapshot.overall_status,
            "breached": [item.name for item in snapshot.slos if item.status == "breached"],
        }
        return facts

    def _process_memory_maintenance(self) -> dict[str, Any]:
        from .memory import MemoryStore

        memory = MemoryStore(self.home)
        gc_facts = memory.garbage_collect()
        graph_facts = memory.sync_semantic_graph(graph_store=self.graph)
        from .workspaces import ShadowWorkspaceManager

        workspace_facts = ShadowWorkspaceManager(self.home).purge_terminal_artifacts()
        return {
            "ok": True,
            "memory_gc": gc_facts,
            "semantic_graph": graph_facts,
            "workspace_gc": workspace_facts,
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
            self._canonical_path(workspace)
            for workspace in self.runs.list_active_workspaces()
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
