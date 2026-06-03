from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .engine import HernessEngine
from .api_paths import api_path
from .auth import AuthInspector
from .app_factory import build_runtime
from .capabilities import CapabilityContext, CapabilityResult, build_capability_registry
from .config import load_config, write_default_config
from .connector_registry import load_connector_adapters
from .daemon import SystemDaemon
from .diagnostics import run_diagnostics
from .defaults import DEFAULT_LOCAL_SURFACE
from .evolution import EvolutionEngine, EvolutionLedger, list_evolution_targets
from .goals import GoalStore
from .graph import GraphStore
from .paths import ensure_home
from .runs import RunStore
from .subagents import SubagentRunStore
from .trace import TraceStore
from .trust import TrustStore
from . import __version__


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class MemoryRequest(BaseModel):
    text: str


class SessionRequest(BaseModel):
    alias: str | None = None


class DelegationRequest(BaseModel):
    title: str
    prompt: str | None = None


class DelegationStatusRequest(BaseModel):
    status: str


class ActiveDelegationRequest(BaseModel):
    prompt: str
    peer_id: str = DEFAULT_LOCAL_SURFACE
    sender_id: str = DEFAULT_LOCAL_SURFACE


class ActiveApprovalRequest(BaseModel):
    code: str
    sender_id: str = DEFAULT_LOCAL_SURFACE


class WatchRequest(BaseModel):
    cron: str
    prompt: str
    peer_id: str = DEFAULT_LOCAL_SURFACE
    sender_id: str = DEFAULT_LOCAL_SURFACE


class ToolCallRequest(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)


class EvolutionProposalRequest(BaseModel):
    target_type: str
    target_id: str
    reason: str
    expected_benefit: str = ""
    risk: str = ""
    before: str = ""
    after: str = ""
    rollback_plan: str = ""
    required_approval_level: str = "L2"
    evidence: str = ""
    source_run_id: str = ""
    eval_cases: list[str] = Field(default_factory=list)


class EvolutionEvaluationRequest(BaseModel):
    evaluation_result: str


def create_app(home: Path | None = None) -> FastAPI:
    home = home or ensure_home()
    write_default_config(home)
    runtime = build_runtime(home)
    task_store = RunStore(home)
    goal_store = GoalStore(home)
    subagent_store = SubagentRunStore(home)
    daemon = SystemDaemon(home)
    agent = HernessEngine(home=home, runtime=runtime, project_dir=Path.cwd())
    capabilities = build_capability_registry(home, project_dir=Path.cwd())
    connector_adapters = load_connector_adapters()
    connector_status_handlers = {
        adapter.name: (lambda item=adapter: item.status(home))
        for adapter in connector_adapters
    }
    app = FastAPI(title="Navi", version=__version__)

    api_key = os.environ.get("NAVI_API_KEY")
    if not api_key:
        api_key_path = home / "api_key"
        if api_key_path.exists():
            api_key = api_key_path.read_text(encoding="utf-8").strip()
        else:
            api_key = secrets.token_hex(32)
            api_key_path.write_text(api_key, encoding="utf-8")
            api_key_path.chmod(0o600)

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        header_key = request.headers.get("X-API-Key")
        if header_key != api_key:
            return Response(content="Unauthorized", status_code=401)
        return await call_next(request)

    @app.get(api_path("health"))
    def health() -> dict:
        config = load_config(home)
        return {
            "ok": True,
            "home": str(home),
            "model_provider": config.model.provider,
            "connectors": {
                adapter.name: {"enabled": adapter.enabled(home)}
                for adapter in connector_adapters
            },
        }

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
            "message": result.text,
            "action": result.action,
            "run_id": result.run_id,
        }

    @app.get(api_path("sessions"))
    def sessions() -> dict:
        return {"sessions": runtime.memory.list_sessions()}

    @app.post(api_path("sessions"))
    def create_session(request: SessionRequest) -> dict:
        session_id = runtime.memory.create_session(alias=request.alias)
        return {"session_id": session_id, "alias": request.alias}

    @app.get(api_path("session_aliases"))
    def session_aliases() -> dict:
        return {"aliases": [alias.__dict__ for alias in runtime.memory.list_session_aliases()]}

    @app.get(api_path("session"))
    def session(session_id: str) -> dict:
        return {"messages": [message.__dict__ for message in runtime.memory.get_messages(session_id)]}

    @app.get(api_path("memory"))
    def get_memory() -> dict:
        return {"memory": runtime.memory.read_memory()}

    @app.post(api_path("memory"))
    def add_memory(request: MemoryRequest) -> dict:
        runtime.memory.append_memory(request.text)
        return {"ok": True}

    @app.get(api_path("skills"))
    def skills() -> dict:
        return {"skills": [skill.__dict__ | {"path": str(skill.path)} for skill in runtime.skills.list_skills()]}

    @app.get(api_path("delegations"))
    def list_delegations() -> dict:
        return {"delegations": [task.__dict__ for task in task_store.list()]}

    @app.post(api_path("delegations"))
    async def create_delegation(request: DelegationRequest) -> dict:
        result = await capabilities.invoke(
            "delegate.spawn",
            {"prompt": request.prompt or request.title},
            permission="prepare",
            context=_local_capability_context(home),
        )
        _raise_capability_error(result)
        prepared = await capabilities.invoke(
            "delegate.prepare",
            {"run_id": result.run_id},
            permission="prepare",
            context=_local_capability_context(home),
        )
        _raise_capability_error(prepared)
        requested = await capabilities.invoke(
            "approval.request",
            {"run_id": result.run_id},
            permission="prepare",
            context=_local_capability_context(home),
        )
        _raise_capability_error(requested)
        task = task_store.get(result.run_id) if result.run_id else None
        if task is None:
            raise HTTPException(status_code=500, detail="delegate.spawn did not return a delegation run")
        return task.__dict__

    @app.patch(api_path("delegation"))
    async def update_delegation(run_id: str, request: DelegationStatusRequest) -> dict:
        decision_by_status = {"queued": "approve", "rejected": "reject"}
        decision = decision_by_status.get(request.status)
        if decision is None:
            raise HTTPException(
                status_code=409,
                detail="delegation status transitions must go through delegation capabilities",
            )
        result = await capabilities.invoke(
            "approval.resolve",
            {"decision": decision, "run_id": run_id},
            permission="write",
            context=_local_capability_context(home),
        )
        _raise_capability_error(result)
        task = task_store.get(run_id)
        if task is None:
            raise HTTPException(status_code=404, detail="delegation run not found")
        return task.__dict__

    @app.delete(api_path("delegation"))
    async def delete_delegation(run_id: str) -> dict:
        result = await capabilities.invoke(
            "delegate.delete",
            {"run_id": run_id},
            permission="write",
            context=_local_capability_context(home),
        )
        if not result.ok and "not found" in result.message:
            raise HTTPException(status_code=404, detail=result.message)
        _raise_capability_error(result)
        return {"deleted": True, "delegation": result.facts}

    @app.get(api_path("approvals"))
    def list_approvals() -> dict:
        return {"approvals": [_public_approval(approval) for approval in task_store.list_approvals()]}

    @app.get(api_path("watches"))
    def list_watches() -> dict:
        return {"watches": [watch.__dict__ for watch in task_store.list_watches()]}

    @app.post(api_path("delegation_approve"))
    async def approve_delegation(run_id: str) -> dict:
        result = await capabilities.invoke(
            "approval.resolve",
            {"decision": "approve", "run_id": run_id},
            permission="write",
            context=_local_capability_context(home),
        )
        if not result.ok and "not found" in result.message.lower():
            raise HTTPException(status_code=409, detail=result.message)
        _raise_capability_error(result)
        task = task_store.get(run_id)
        if task is None:
            raise HTTPException(status_code=404, detail="delegation run not found")
        return task.__dict__

    @app.post(api_path("delegations_process"))
    async def process_delegations() -> dict:
        return {"delegations": [task.__dict__ for task in await daemon.process_queue_once()]}

    @app.post(api_path("active_delegations"))
    async def create_active_delegation(request: ActiveDelegationRequest) -> dict:
        context = CapabilityContext(
            home=home,
            peer_id=request.peer_id,
            sender_id=request.sender_id,
            source=load_config(home).runtime.local_surface,
        )
        result = await capabilities.invoke(
            "delegate.spawn",
            {"prompt": request.prompt},
            permission="prepare",
            context=context,
        )
        if result.ok:
            await capabilities.invoke(
                "delegate.prepare",
                {"run_id": result.run_id},
                permission="prepare",
                context=context,
            )
            result = await capabilities.invoke(
                "approval.request",
                {"run_id": result.run_id},
                permission="prepare",
                context=context,
            )
        task = task_store.get(result.run_id) if result.run_id else None
        approval = task_store.pending_approval_for_run(result.run_id, sender_id=request.sender_id) if result.run_id else None
        message = result.message or result.observation
        if task and approval:
            message = (
                f"Delegation run `{task.id}` is prepared for approval.\n"
                f"Preparation:\n{task.plan_summary or '(no preparation output)'}\n\n"
                f"Approval expires in 15 minutes.\n"
                f"Approval code: `{approval.code}`.\n"
                f"Reply with `approve {approval.code}` or `reject {approval.code}`."
            )
        return {"message": message, "delegation": task.__dict__ if task else None}

    @app.post(api_path("active_approve"))
    async def approve_active_delegation(request: ActiveApprovalRequest) -> dict:
        result = await capabilities.invoke(
            "approval.resolve",
            {"decision": "approve", "code": request.code},
            permission="write",
            context=CapabilityContext(
                home=home,
                sender_id=request.sender_id,
                source=load_config(home).runtime.local_surface,
            ),
        )
        task = task_store.get(result.run_id) if result.run_id else None
        return {"message": result.message or result.observation, "delegation": task.__dict__ if task else None}

    @app.post(api_path("active_reject"))
    async def reject_active_delegation(request: ActiveApprovalRequest) -> dict:
        result = await capabilities.invoke(
            "approval.resolve",
            {"decision": "reject", "code": request.code},
            permission="write",
            context=CapabilityContext(
                home=home,
                sender_id=request.sender_id,
                source=load_config(home).runtime.local_surface,
            ),
        )
        return {"message": result.message or result.observation}

    @app.post(api_path("active_watches"))
    async def create_active_watch(request: WatchRequest) -> dict:
        result = await capabilities.invoke(
            "watch.create",
            {"cron": request.cron, "prompt": request.prompt},
            permission="prepare",
            context=CapabilityContext(
                home=home,
                peer_id=request.peer_id,
                sender_id=request.sender_id,
                source=load_config(home).runtime.local_surface,
            ),
        )
        watch_id = str((result.facts or {}).get("watch_id") or "")
        watch = task_store.get_watch(watch_id) if watch_id else None
        message = result.message or result.observation
        if watch:
            message = (
                f"Watch `{watch.id}` created.\n"
                f"Cron: {watch.cron}\n"
                f"Request: {watch.prompt}\n"
                f"Next run at {__import__('time').ctime(watch.next_run_at)}."
            )
        return {"message": message}

    @app.post(api_path("active_watches_process"))
    async def process_watches() -> dict:
        return {"results": await daemon.process_watches_once()}

    @app.get(api_path("auth_status"))
    def auth_status() -> dict:
        return {"providers": [item.__dict__ for item in AuthInspector().status()]}

    @app.get(api_path("diagnostics"))
    def diagnostics(connectivity: bool = False) -> dict:
        return {
            "checks": [
                check.__dict__
                for check in run_diagnostics(home, project_dir=Path.cwd(), include_connectivity=connectivity)
            ]
        }

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
                    f"{spec.permission} and must use managed approval flow"
                ),
            )
        result = await capabilities.invoke(
            tool_name,
            request.args,
            permission=spec.permission,
            context=_local_capability_context(home),
        )
        return _capability_result_dict(result)

    @app.get(api_path("graph"))
    def graph() -> dict:
        return {"nodes": [node.__dict__ for node in GraphStore(home).list()]}

    @app.get(api_path("trust_rules"))
    def trust_rules() -> dict:
        return {"trust_rules": [rule.__dict__ for rule in TrustStore(home).list()]}

    @app.get(api_path("traces"))
    def traces() -> dict:
        return {"trace_ids": TraceStore(home).list_trace_ids()}

    @app.get(api_path("trace"))
    def trace(trace_id: str) -> dict:
        return {"events": [event.__dict__ for event in TraceStore(home).list_events(trace_id)]}

    @app.post(api_path("trace_evaluate"))
    def trace_evaluate(trace_id: str) -> dict:
        return TraceStore(home).evaluate_trace(trace_id).__dict__

    @app.get(api_path("goals"))
    def list_goals(status: str = "", limit: int = 50) -> dict:
        return {"goals": [goal.__dict__ for goal in goal_store.list(status=status, limit=limit)]}

    @app.get(api_path("goal"))
    def get_goal(goal_id: str) -> dict:
        goal = goal_store.get(goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="goal not found")
        return {
            "goal": goal.__dict__,
            "events": [event.__dict__ for event in goal_store.list_events(goal_id)],
        }

    @app.get(api_path("subagents"))
    def list_subagents(role: str = "", status: str = "", run_id: str = "", limit: int = 50) -> dict:
        return {
            "subagents": [
                subagent.__dict__
                for subagent in subagent_store.list(role=role, status=status, run_id=run_id, limit=limit)
            ]
        }

    @app.get(api_path("subagent"))
    def get_subagent(subagent_id: str) -> dict:
        subagent = subagent_store.get(subagent_id)
        if subagent is None:
            raise HTTPException(status_code=404, detail="subagent run not found")
        return {"subagent": subagent.__dict__}

    @app.get(api_path("evolution_events"))
    def evolution_events() -> dict:
        return {"events": [event.__dict__ for event in EvolutionLedger(home).list()]}

    @app.get(api_path("evolution_targets"))
    def evolution_targets() -> dict:
        return {"targets": list_evolution_targets()}

    @app.get(api_path("evolution_proposals"))
    def evolution_proposals(status: str | None = None) -> dict:
        return {"proposals": [proposal.__dict__ for proposal in EvolutionLedger(home).list_proposals(status=status)]}

    @app.post(api_path("evolution_proposals"))
    def create_evolution_proposal(request: EvolutionProposalRequest) -> dict:
        try:
            data = request.model_dump()
            data["source_run_id"] = data.pop("source_run_id", "")
            proposal = EvolutionLedger(home).propose(**data)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return proposal.__dict__

    @app.post(api_path("evolution_proposal_apply"))
    def apply_evolution_proposal(proposal_id: str) -> dict:
        try:
            event = EvolutionEngine(home).apply_proposal(proposal_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if event is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        return event.__dict__

    @app.post(api_path("evolution_proposal_evaluation"))
    def record_evolution_proposal_evaluation(proposal_id: str, request: EvolutionEvaluationRequest) -> dict:
        proposal = EvolutionLedger(home).record_proposal_evaluation(proposal_id, request.evaluation_result)
        if proposal is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        return proposal.__dict__

    @app.post(api_path("evolution_rollback"))
    def rollback_evolution(event_id: str) -> dict:
        event = EvolutionEngine(home).rollback(event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="event not found")
        return event.__dict__

    @app.get(api_path("connector_status"))
    def connector_status(connector_name: str) -> dict:
        handler = connector_status_handlers.get(connector_name)
        if handler is None:
            raise HTTPException(status_code=404, detail="connector not found")
        return handler()

    return app


def _public_approval(approval) -> dict:
    return {
        "id": approval.id,
        "run_id": approval.run_id,
        "action": approval.action,
        "peer_id": approval.peer_id,
        "sender_id": approval.sender_id,
        "status": approval.status,
        "expires_at": approval.expires_at,
        "created_at": approval.created_at,
        "updated_at": approval.updated_at,
        "code_present": bool(approval.code),
    }


def _local_capability_context(home: Path) -> CapabilityContext:
    local_surface = load_config(home).runtime.local_surface
    return CapabilityContext(
        home=home,
        peer_id=local_surface,
        sender_id=local_surface,
        source=local_surface,
    )


def _raise_capability_error(result: CapabilityResult) -> None:
    if result.ok:
        return
    raise HTTPException(status_code=409, detail=result.message or result.observation)


def _capability_result_dict(result: CapabilityResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "action": result.action,
        "observation": result.observation,
        "message": result.message,
        "run_id": result.run_id,
        "terminal": result.terminal,
        "facts": result.facts or {},
    }
