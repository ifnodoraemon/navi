from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .api_paths import api_path
from .app_factory import build_runtime
from .approval_contract import APPROVAL_DECISION_APPROVE, APPROVAL_DECISION_REJECT
from .auth import AuthInspector
from .capabilities import CapabilityContext, CapabilityResult, build_capability_registry
from .config import load_config, write_default_config
from .connector_registry import load_connector_adapters
from .control_plane import TurnController
from .daemon import SystemDaemon
from .defaults import DEFAULT_LOCAL_SURFACE
from .diagnostics import run_diagnostics
from .evolution import EvolutionLedger, list_evolution_targets
from .goals import GoalStore
from .graph import GraphStore
from .json_utils import json_object
from .paths import ensure_home
from .runs import RunStore
from .service import runtime_environment_error
from .tools import API_CONTEXT
from .trace import TraceStore


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class MemoryRequest(BaseModel):
    memory_type: str = Field(alias="type")
    content: str
    scope: str = "global"
    source: str = "api"
    status: str = "proposed"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reason: str = Field(min_length=1)
    provenance: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionRequest(BaseModel):
    alias: str | None = None


class ActiveApprovalRequest(BaseModel):
    code: str
    sender_id: str = DEFAULT_LOCAL_SURFACE


class GoalOpenRequest(BaseModel):
    objective: str
    workspace: str | None = None
    loop_kind: str = "durable_goal"
    scope: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    permission_ceiling: str | None = None
    allowed_capabilities: list[str] = Field(default_factory=list)
    verification_command: str | None = None
    timeout_seconds: int | None = None
    token_budget: int | None = None
    call_budget: int | None = None
    cost_budget: float | None = None
    qps_limit: int | None = None
    max_concurrent: int | None = None
    auto_start: bool = True


class GoalResumeRequest(BaseModel):
    workspace: str | None = None
    loop_run_id: str | None = None


class GoalCancelRequest(BaseModel):
    reason: str = ""
    loop_run_id: str | None = None


class ToolCallRequest(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)


class EvolutionProposalRequest(BaseModel):
    target_type: str
    target_id: str
    reason: str
    expected_benefit: str
    risk: str = ""
    after: str
    rollback_plan: str
    required_approval_level: str = "L2"
    evidence: str = ""
    source_run_id: str = ""
    eval_cases: list[str]


class EvolutionEvaluationRequest(BaseModel):
    evaluation_result: str
    evaluation_evidence: str = ""
    approval_id: str = ""


class EvolutionObservationRequest(BaseModel):
    successes: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    evidence: dict[str, Any] = Field(default_factory=dict)


def _is_public_request(request: Request) -> bool:
    path = request.url.path.rstrip("/") or "/"
    if path == "/ui/trace" or path.startswith("/ui/trace/"):
        return True
    traces_path = api_path("traces")
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return path == traces_path or path.startswith(f"{traces_path}/")
    return False


def _build_lifespan(
    *,
    home: Path,
    project_dir: Path,
    daemon: SystemDaemon,
    connector_adapters,
    start_background: bool,
    start_connectors: bool,
):
    async def run_adapter(adapter) -> None:
        try:
            await adapter.run(home, project_dir, False)
        except Exception:
            import logging

            logging.getLogger(__name__).exception("ERROR STARTING ADAPTER %s", adapter.name)

    async def run_background() -> None:
        while True:
            try:
                await daemon.process_background_once()
                await daemon.process_queue_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                import logging

                logging.getLogger(__name__).exception("Background processing failed")
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        del app
        setup_tasks = [
            adapter.setup(home, project_dir, 60, None)
            for adapter in connector_adapters
            if start_connectors and adapter.setup and adapter.enabled(home)
        ]
        if setup_tasks:
            await asyncio.gather(*setup_tasks, return_exceptions=True)
        run_tasks: list[asyncio.Task] = []
        if start_background:
            run_tasks.append(asyncio.create_task(run_background()))
        run_tasks.extend(
            asyncio.create_task(run_adapter(adapter))
            for adapter in connector_adapters
            if start_connectors and adapter.run and adapter.enabled(home)
        )
        try:
            yield
        finally:
            for task in run_tasks:
                task.cancel()
            await asyncio.gather(*run_tasks, return_exceptions=True)

    return lifespan


def _register_middleware(app: FastAPI, *, api_key: str) -> None:
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if _is_public_request(request):
            return await call_next(request)
        provided_key = request.headers.get("X-API-Key", "")
        if not provided_key or not secrets.compare_digest(provided_key, api_key):
            return Response("Unauthorized", status_code=401)
        return await call_next(request)

    @app.middleware("http")
    async def envelope_middleware(request: Request, call_next):
        response: Response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("application/json"):
            return response
        chunks: list[bytes] = []
        if hasattr(response, "body_iterator"):
            async for chunk in response.body_iterator:
                chunks.append(chunk)
        else:
            chunks.append(getattr(response, "body", b""))
        body = b"".join(chunks)
        if not body:
            return response
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            return Response(
                content=body,
                status_code=response.status_code,
                media_type="application/json",
            )
        status_code = response.status_code
        if 200 <= status_code < 300:
            envelope: dict[str, Any] = {"ok": True, "data": parsed, "error": None}
        else:
            detail = parsed.get("detail") if isinstance(parsed, dict) else parsed
            envelope = {
                "ok": False,
                "data": None,
                "error": {"status": status_code, "detail": detail},
            }
        return JSONResponse(content=envelope, status_code=status_code)


def _health_response(home: Path, connector_adapters) -> dict | JSONResponse:
    config = load_config(home)
    runtime_error = runtime_environment_error()
    connectors: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, str]] = []
    if runtime_error:
        issues.append({"component": "runtime", "error": runtime_error})
    for adapter in connector_adapters:
        if not adapter.enabled(home):
            connectors[adapter.name] = {"enabled": False, "status": "disabled"}
            continue
        facts = adapter.status(home)
        connector_status = str(facts.get("status") or "unknown")
        connectors[adapter.name] = {
            "enabled": True,
            "status": connector_status,
            "ingress_status": str(facts.get("ingress_status") or "unknown"),
            "egress_status": str(facts.get("egress_status") or "unknown"),
            "proactive_egress_status": str(facts.get("proactive_egress_status") or "unknown"),
            "delivery_incident_status": str(
                facts.get("delivery_incident_status") or "unknown"
            ),
            "proactive_delivery_windows": facts.get("proactive_delivery_windows") or {},
        }
        if connector_status != "healthy":
            issues.append(
                {"component": f"connector.{adapter.name}", "status": connector_status}
            )
    payload = {
        "ok": not issues,
        "home": str(home),
        "model_provider": config.model.provider,
        "runtime": {
            "status": "healthy" if not runtime_error else "unavailable",
            "error": runtime_error,
        },
        "connectors": connectors,
        "issues": issues,
    }
    if issues:
        return JSONResponse(status_code=503, content={"detail": payload})
    return payload


def _register_tool_routes(
    app: FastAPI,
    *,
    home: Path,
    project_dir: Path,
    capabilities,
) -> None:
    @app.get(api_path("tools"))
    def list_tools() -> dict:
        return {
            "tools": [asdict(spec) for spec in capabilities.list_specs()],
            "capabilities": [asdict(node) for node in capabilities.capability_graph()],
            "sources": capabilities.list_sources(),
        }

    @app.post(api_path("tool_call"))
    async def call_tool(tool_name: str, request: ToolCallRequest) -> dict:
        spec = capabilities.get(tool_name)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"capability not found: {tool_name}")
        if spec.permission != "read":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"direct API tool calls are read-only; {tool_name} requires "
                    f"{spec.permission} permission"
                ),
            )
        result = await capabilities.invoke(
            tool_name,
            request.args,
            permission=spec.permission,
            context=_local_capability_context(home, project_dir=project_dir),
        )
        return _capability_result_dict(result)


def create_app(
    home: Path | None = None,
    *,
    start_background: bool = False,
    start_connectors: bool = False,
) -> FastAPI:
    home = home or ensure_home()
    project_dir = Path.cwd().resolve()
    write_default_config(home)
    config = load_config(home)
    api_key = config.api.api_key
    if not api_key:
        raise ValueError("api.api_key is required")
    runtime = build_runtime(home)
    task_store = RunStore(home)
    daemon = SystemDaemon(home, project_dir=project_dir)
    agent = TurnController(
        home=home, runtime=runtime, project_dir=project_dir, event_bus=daemon.event_bus
    )
    capabilities = build_capability_registry(home, project_dir=project_dir, runtime=runtime)
    api_capabilities = build_capability_registry(
        home,
        project_dir=project_dir,
        execution_context=API_CONTEXT,
        runtime=runtime,
    )
    connector_adapters = load_connector_adapters()

    lifespan = _build_lifespan(
        home=home,
        project_dir=project_dir,
        daemon=daemon,
        connector_adapters=connector_adapters,
        start_background=start_background,
        start_connectors=start_connectors,
    )
    app = FastAPI(title="Navi", version=__version__, lifespan=lifespan)
    _register_middleware(app, api_key=api_key)

    @app.get(api_path("health"), response_model=None)
    def health() -> dict | JSONResponse:
        return _health_response(home, connector_adapters)

    @app.post(api_path("chat"))
    async def chat(request: ChatRequest) -> dict:
        config = load_config(home)
        result = await agent.handle(
            request.message,
            peer_id=config.runtime.local_surface,
            sender_id=config.runtime.local_surface,
            source=config.runtime.local_surface,
            session_id=request.session_id,
        )
        return {
            "session_id": result.session_id,
            "message": result.surfaced_text(),
            "action": result.action,
            "run_id": result.run_id,
            "facts": result.facts or {},
        }

    @app.get(api_path("approvals"))
    def list_approvals() -> dict:
        return {
            "approvals": [_public_approval(approval) for approval in task_store.list_approvals()]
        }

    @app.post(api_path("active_approve"))
    async def approve_active_delegation(request: ActiveApprovalRequest) -> dict:
        result = await api_capabilities.invoke(
            "approval.resolve",
            {"decision": APPROVAL_DECISION_APPROVE, "code": request.code},
            permission="write",
            context=CapabilityContext(
                home=home,
                sender_id=request.sender_id,
                source=load_config(home).runtime.local_surface,
                workspace=str(project_dir),
            ),
        )
        task = task_store.get(result.run_id) if result.run_id else None
        return {
            "message": _local_result_message(
                result, source=load_config(home).runtime.local_surface
            ),
            "delegation": task.__dict__ if task else None,
            "facts": result.facts or {},
        }

    @app.post(api_path("active_reject"))
    async def reject_active_delegation(request: ActiveApprovalRequest) -> dict:
        result = await api_capabilities.invoke(
            "approval.resolve",
            {"decision": APPROVAL_DECISION_REJECT, "code": request.code},
            permission="write",
            context=CapabilityContext(
                home=home,
                sender_id=request.sender_id,
                source=load_config(home).runtime.local_surface,
                workspace=str(project_dir),
            ),
        )
        return {
            "message": _local_result_message(
                result, source=load_config(home).runtime.local_surface
            ),
            "facts": result.facts or {},
        }

    @app.get(api_path("auth_status"))
    def auth_status() -> dict:
        return {"providers": [item.__dict__ for item in AuthInspector().status()]}

    @app.get(api_path("diagnostics"))
    def diagnostics(connectivity: bool = False) -> dict:
        return {
            "checks": [
                check.__dict__
                for check in run_diagnostics(
                    home, project_dir=project_dir, include_connectivity=connectivity
                )
            ]
        }

    @app.get(api_path("metrics"))
    def metrics() -> dict:
        from .metrics import MetricsProjector

        return MetricsProjector(home).snapshot().to_dict()

    _register_tool_routes(
        app,
        home=home,
        project_dir=project_dir,
        capabilities=capabilities,
    )
    _register_state_routes(
        app,
        home=home,
        project_dir=project_dir,
        runtime=runtime,
        api_capabilities=api_capabilities,
        connector_adapters=connector_adapters,
    )
    return app


def _register_state_routes(
    app: FastAPI,
    *,
    home: Path,
    project_dir: Path,
    runtime,
    api_capabilities,
    connector_adapters,
) -> None:
    goal_store = GoalStore(home)
    connector_status_handlers = {
        adapter.name: (lambda item=adapter: item.status(home)) for adapter in connector_adapters
    }

    @app.get(api_path("sessions"))
    def sessions() -> dict:
        return {"sessions": runtime.memory.list_sessions()}

    @app.post(api_path("sessions"))
    async def create_session(request: SessionRequest) -> dict:
        result = await api_capabilities.invoke(
            "session.create",
            request.model_dump(exclude_none=True),
            permission="prepare",
            context=_local_capability_context(home, project_dir=project_dir),
        )
        _raise_capability_error(result)
        facts = result.facts or {}
        return {"session_id": facts.get("session_id", ""), "alias": facts.get("alias", "")}

    @app.get(api_path("session_aliases"))
    def session_aliases() -> dict:
        return {"aliases": [alias.__dict__ for alias in runtime.memory.list_session_aliases()]}

    @app.get(api_path("session"))
    def session(session_id: str) -> dict:
        return {
            "messages": [message.__dict__ for message in runtime.memory.get_messages(session_id)]
        }

    @app.get(api_path("memory"))
    def get_memory(
        memory_type: str | None = None, status: str | None = None, limit: int = 50
    ) -> dict:
        items = runtime.memory.list_items(memory_type=memory_type, status=status, limit=limit)
        return {"items": [asdict(item) for item in items]}

    @app.get(api_path("memory_conflicts"))
    def get_memory_conflicts(limit: int = 50) -> dict:
        conflicts = runtime.memory.list_conflicts(limit=limit)
        return {
            "conflicts": [asdict(conflict) for conflict in conflicts],
            "count": len(conflicts),
            "unresolved_count": len(
                [conflict for conflict in conflicts if conflict.status == "unresolved"]
            ),
        }

    @app.post(api_path("memory"))
    async def add_memory(request: MemoryRequest) -> dict:
        result = await api_capabilities.invoke(
            "memory.add",
            request.model_dump(by_alias=True),
            permission="write",
            context=_local_capability_context(home, project_dir=project_dir),
        )
        _raise_capability_error(result)
        return {"item": (result.facts or {}).get("item", {})}

    @app.get(api_path("skills"))
    def skills() -> dict:
        return {
            "skills": [
                skill.__dict__ | {"path": str(skill.path)} for skill in runtime.skills.list_skills()
            ]
        }

    @app.get(api_path("graph"))
    def graph() -> dict:
        return {"nodes": [node.__dict__ for node in GraphStore(home).list()]}

    @app.get(api_path("traces"))
    def traces(
        limit: int = 50, offset: int = 0, has_error: bool | None = None, query: str = ""
    ) -> dict:
        return {
            "traces": TraceStore(home).list_trace_meta(
                limit=limit, offset=offset, has_error=has_error, query=query
            )
        }

    @app.delete(api_path("traces"))
    async def delete_all_traces() -> dict:
        result = await api_capabilities.invoke(
            "trace.delete",
            {"all": True},
            permission="write",
            context=_local_capability_context(home, project_dir=project_dir),
        )
        _raise_capability_error(result)
        return result.facts or {}

    @app.delete(api_path("traces") + "/{trace_id}")
    async def delete_trace(trace_id: str) -> dict:
        result = await api_capabilities.invoke(
            "trace.delete",
            {"trace_id": trace_id},
            permission="write",
            context=_local_capability_context(home, project_dir=project_dir),
        )
        _raise_capability_error(result)
        return result.facts or {}

    @app.get(api_path("trace"))
    def trace(trace_id: str, limit: int = 5000, offset: int = 0) -> dict:
        store = TraceStore(home)
        events_page = store.list_events(trace_id, limit=limit, offset=offset)

        loop_decisions = [
            {
                **event.__dict__,
                "decision": json_object(event.output_json),
            }
            for event in store.list_loop_decisions(trace_id, limit=limit, offset=offset)
        ]

        return {
            "events": [event.__dict__ for event in events_page],
            "runs": [run.to_dict() for run in store.list_run_views(trace_id)],
            "loop_decisions": loop_decisions,
            "loop_runs": store.list_loop_run_details(trace_id),
            "evaluations": [item.to_dict() for item in store.list_evaluations(trace_id)],
        }

    @app.get(api_path("trace_decisions"))
    def trace_decisions(trace_id: str) -> dict:
        store = TraceStore(home)
        return {
            "loop_decisions": [
                {
                    **event.__dict__,
                    "decision": json_object(event.output_json),
                }
                for event in store.list_loop_decisions(trace_id)
            ]
        }

    @app.get(api_path("trace_runs"))
    def trace_runs(trace_id: str) -> dict:
        store = TraceStore(home)
        return {
            "runs": [run.to_dict() for run in store.list_run_views(trace_id)],
            "loop_runs": store.list_loop_run_details(trace_id),
        }

    @app.get(api_path("trace_evaluations"))
    def trace_evaluations(trace_id: str = "", limit: int = 50) -> dict:
        return {
            "evaluations": [
                item.to_dict() for item in TraceStore(home).list_evaluations(trace_id, limit=limit)
            ]
        }

    @app.post(api_path("trace_evaluate"))
    async def trace_evaluate(trace_id: str) -> dict:
        result = await api_capabilities.invoke(
            "trace.evaluate",
            {"trace_id": trace_id},
            permission="write",
            context=_local_capability_context(home, project_dir=project_dir),
        )
        _raise_capability_error(result)
        return (result.facts or {}).get("evaluation", {})

    @app.get(api_path("goals"))
    def list_goals(phase: str = "", limit: int = 50) -> dict:
        return {"goals": [goal.__dict__ for goal in goal_store.list(phase=phase, limit=limit)]}

    @app.post(api_path("goals"))
    async def open_goal(request: GoalOpenRequest) -> dict:
        args = request.model_dump(exclude_none=True)
        if not args.get("workspace"):
            args["workspace"] = str(project_dir)
        context = _local_capability_context(
            home,
            project_dir=project_dir,
            workspace=str(args["workspace"]),
        )
        result = await api_capabilities.invoke(
            "goal.open",
            args,
            permission="prepare",
            context=context,
        )
        _raise_capability_error(result)
        return _capability_result_dict(result)

    @app.get(api_path("goal"))
    def get_goal(goal_id: str) -> dict:
        goal = goal_store.get(goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="goal not found")
        return {
            "goal": goal.__dict__,
            "events": [event.__dict__ for event in goal_store.list_events(goal_id)],
        }

    @app.post(api_path("goal_resume"))
    async def resume_goal(goal_id: str, request: GoalResumeRequest) -> dict:
        args = request.model_dump(exclude_none=True)
        args["goal_id"] = goal_id
        if not args.get("workspace"):
            args["workspace"] = str(project_dir)
        context = _local_capability_context(
            home,
            project_dir=project_dir,
            workspace=str(args["workspace"]),
        )
        result = await api_capabilities.invoke(
            "goal.resume",
            args,
            permission="prepare",
            context=context,
        )
        _raise_capability_error(result, not_found_status=404)
        return _capability_result_dict(result)

    @app.post(api_path("goal_cancel"))
    async def cancel_goal(goal_id: str, request: GoalCancelRequest) -> dict:
        args = request.model_dump(exclude_none=True)
        args["goal_id"] = goal_id
        goal = goal_store.get(goal_id)
        workspace = goal.workspace if goal is not None else str(project_dir)
        context = _local_capability_context(home, project_dir=project_dir, workspace=workspace)
        result = await api_capabilities.invoke(
            "goal.cancel",
            args,
            permission="prepare",
            context=context,
        )
        _raise_capability_error(result, not_found_status=404)
        return _capability_result_dict(result)

    @app.get(api_path("goal_state"))
    async def goal_state(goal_id: str) -> dict:
        goal = goal_store.get(goal_id)
        workspace = goal.workspace if goal is not None else str(project_dir)
        context = _local_capability_context(home, project_dir=project_dir, workspace=workspace)
        result = await api_capabilities.invoke(
            "goal.state",
            {"goal_id": goal_id},
            permission="read",
            context=context,
        )
        _raise_capability_error(result, not_found_status=404)
        return _capability_result_dict(result)

    @app.get(api_path("evolution_events"))
    def evolution_events() -> dict:
        return {"events": [event.__dict__ for event in EvolutionLedger(home).list()]}

    @app.get(api_path("evolution_targets"))
    def evolution_targets() -> dict:
        return {"targets": list_evolution_targets()}

    @app.get(api_path("evolution_proposals"))
    def evolution_proposals(status: str | None = None) -> dict:
        proposals = EvolutionLedger(home).list_proposals(status=status)
        return {
            "proposals": [
                p.__dict__
                for p in proposals  # type: ignore[union-attr]
            ]
            if proposals
            else []
        }

    @app.post(api_path("evolution_proposals"))
    async def create_evolution_proposal(request: EvolutionProposalRequest) -> dict:
        result = await api_capabilities.invoke(
            "evolution.propose",
            request.model_dump(),
            permission="prepare",
            context=_local_capability_context(home, project_dir=project_dir),
        )
        _raise_capability_error(result)
        return (result.facts or {}).get("proposal", {})

    @app.post(api_path("evolution_proposal_apply"))
    async def apply_evolution_proposal(proposal_id: str) -> dict:
        result = await api_capabilities.invoke(
            "evolution.apply",
            {"proposal_id": proposal_id},
            permission="write",
            context=_local_capability_context(home, project_dir=project_dir),
        )
        _raise_capability_error(result, not_found_status=404)
        return (result.facts or {}).get("event", {})

    @app.post(api_path("evolution_proposal_experiment"))
    async def experiment_evolution_proposal(proposal_id: str) -> dict:
        result = await api_capabilities.invoke(
            "evolution.experiment",
            {"proposal_id": proposal_id},
            permission="prepare",
            context=_local_capability_context(home, project_dir=project_dir),
        )
        _raise_capability_error(result, not_found_status=404)
        return (result.facts or {}).get("experiment", {})

    @app.post(api_path("evolution_proposal_evaluation"))
    async def record_evolution_proposal_evaluation(
        proposal_id: str, request: EvolutionEvaluationRequest
    ) -> dict:
        result = await api_capabilities.invoke(
            "evolution.record_evaluation",
            {
                "proposal_id": proposal_id,
                "evaluation_result": request.evaluation_result,
                "evaluation_evidence": request.evaluation_evidence,
                "approval_id": request.approval_id,
            },
            permission="write",
            context=_local_capability_context(home, project_dir=project_dir),
        )
        _raise_capability_error(result, not_found_status=404)
        return (result.facts or {}).get("proposal", {})

    @app.post(api_path("evolution_rollback"))
    async def rollback_evolution(event_id: str) -> dict:
        result = await api_capabilities.invoke(
            "evolution.rollback",
            {"event_id": event_id},
            permission="write",
            context=_local_capability_context(home, project_dir=project_dir),
        )
        _raise_capability_error(result, not_found_status=404)
        return (result.facts or {}).get("event", {})

    @app.post(api_path("evolution_observe"))
    async def observe_evolution_activation(
        event_id: str, request: EvolutionObservationRequest
    ) -> dict:
        result = await api_capabilities.invoke(
            "evolution.observe",
            {"event_id": event_id, **request.model_dump()},
            permission="write",
            context=_local_capability_context(home, project_dir=project_dir),
        )
        _raise_capability_error(result, not_found_status=404)
        return (result.facts or {}).get("activation", {})

    @app.get(api_path("connector_status"))
    def connector_status(connector_name: str) -> dict:
        handler = connector_status_handlers.get(connector_name)
        if handler is None:
            raise HTTPException(status_code=404, detail="connector not found")
        return handler()

    dist_dir = Path(__file__).resolve().parent / "static" / "trace"
    if dist_dir.exists():
        app.mount("/ui/trace", StaticFiles(directory=str(dist_dir), html=True), name="trace_ui")

    return None


def _public_approval(approval) -> dict:
    return {
        "id": approval.id,
        "run_id": approval.run_id,
        "action": approval.action,
        "requested_tool": approval.requested_tool,
        "requested_permission": approval.requested_permission,
        "peer_id": approval.peer_id,
        "sender_id": approval.sender_id,
        "status": approval.status,
        "expires_at": approval.expires_at,
        "created_at": approval.created_at,
        "updated_at": approval.updated_at,
        "code_present": bool(approval.code),
    }


def _local_capability_context(
    home: Path,
    *,
    project_dir: Path,
    workspace: str | None = None,
) -> CapabilityContext:
    local_surface = load_config(home).runtime.local_surface
    return CapabilityContext(
        home=home,
        peer_id=local_surface,
        sender_id=local_surface,
        source=local_surface,
        permission_ceiling="write",
        workspace=str(workspace or project_dir),
    )


def _raise_capability_error(result: CapabilityResult, *, not_found_status: int = 409) -> None:
    if result.ok:
        return
    # Never echo raw result text (which may contain
    # file paths, shell output, or internal state) into an HTTP response.
    # Prefer the capability's structured message; fall back to a generic
    # detail rather than leaking raw result content.
    safe_detail = result.message or "capability invocation failed"
    if result.error_reason == "not_found":
        raise HTTPException(status_code=not_found_status, detail=safe_detail)
    approval = (result.facts or {}).get("approval")
    if isinstance(approval, dict) and approval.get("id"):
        raise HTTPException(
            status_code=409,
            detail={"message": "approval required", "approval": approval},
        )
    raise HTTPException(status_code=409, detail=safe_detail)


def _capability_result_dict(result: CapabilityResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "action": result.action,
        "message": result.message,
        "run_id": result.run_id,
        "terminal": result.terminal,
        "facts": result.facts or {},
    }


def _local_result_message(result: CapabilityResult, *, source: str) -> str:
    del source
    return result.message
