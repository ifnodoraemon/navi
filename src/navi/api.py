from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from .engine import HernessEngine
from .api_paths import API_PATHS, api_path
from .auth import AuthInspector
from .app_factory import build_runtime
from .capabilities import CapabilityContext, CapabilityResult, build_capability_registry
from .config import load_config, write_default_config
from .connector_registry import load_connector_adapters
from .daemon import SystemDaemon
from .defaults import DEFAULT_LOCAL_SURFACE
from .evolution import EvolutionEngine, EvolutionLedger
from .graph import GraphStore
from .paths import ensure_home
from .tasks import TaskStore
from .trust import TrustStore
from . import __version__


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class MemoryRequest(BaseModel):
    text: str


class SessionRequest(BaseModel):
    alias: str | None = None


class TaskRequest(BaseModel):
    title: str
    prompt: str | None = None


class TaskStatusRequest(BaseModel):
    status: str


class ActiveTaskRequest(BaseModel):
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


def create_app(home: Path | None = None) -> FastAPI:
    home = home or ensure_home()
    write_default_config(home)
    runtime = build_runtime(home)
    task_store = TaskStore(home)
    daemon = SystemDaemon(home)
    agent = HernessEngine(home=home, runtime=runtime, project_dir=Path.cwd())
    capabilities = build_capability_registry(home, project_dir=Path.cwd())
    connector_adapters = load_connector_adapters()
    connector_status_handlers = {
        adapter.name: (lambda item=adapter: item.status(home))
        for adapter in connector_adapters
    }
    app = FastAPI(title="Navi", version=__version__)

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
            "task_id": result.task_id,
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

    @app.get(api_path("tasks"))
    def list_tasks() -> dict:
        return {"tasks": [task.__dict__ for task in task_store.list()]}

    @app.post(api_path("tasks"))
    async def create_task(request: TaskRequest) -> dict:
        result = await capabilities.invoke(
            "task.create",
            {"prompt": request.prompt or request.title},
            permission="prepare",
            context=_local_capability_context(home),
        )
        _raise_capability_error(result)
        task = task_store.get(result.task_id) if result.task_id else None
        if task is None:
            raise HTTPException(status_code=500, detail="task.create did not return a task")
        return task.__dict__

    @app.patch(api_path("task"))
    async def update_task(task_id: str, request: TaskStatusRequest) -> dict:
        decision_by_status = {"queued": "approve", "rejected": "reject"}
        decision = decision_by_status.get(request.status)
        if decision is None:
            raise HTTPException(
                status_code=409,
                detail="task status transitions must go through task capabilities",
            )
        result = await capabilities.invoke(
            "approval.resolve",
            {"decision": decision, "task_id": task_id},
            permission="write",
            context=_local_capability_context(home),
        )
        _raise_capability_error(result)
        task = task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task.__dict__

    @app.delete(api_path("task"))
    async def delete_task(task_id: str) -> dict:
        result = await capabilities.invoke(
            "task.delete",
            {"task_id": task_id},
            permission="write",
            context=_local_capability_context(home),
        )
        if not result.ok and "not found" in result.message:
            raise HTTPException(status_code=404, detail=result.message)
        _raise_capability_error(result)
        return {"deleted": True, "task": result.facts}

    @app.get(api_path("approvals"))
    def list_approvals() -> dict:
        return {"approvals": [_public_approval(approval) for approval in task_store.list_approvals()]}

    @app.get(api_path("watches"))
    def list_watches() -> dict:
        return {"watches": [watch.__dict__ for watch in task_store.list_watches()]}

    @app.post(api_path("task_approve"))
    async def approve_task(task_id: str) -> dict:
        result = await capabilities.invoke(
            "approval.resolve",
            {"decision": "approve", "task_id": task_id},
            permission="write",
            context=_local_capability_context(home),
        )
        if not result.ok and "not found" in result.message.lower():
            raise HTTPException(status_code=409, detail=result.message)
        _raise_capability_error(result)
        task = task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task.__dict__

    @app.post(api_path("tasks_process"))
    async def process_tasks() -> dict:
        return {"tasks": [task.__dict__ for task in await daemon.process_queue_once()]}

    @app.post(api_path("active_tasks"))
    async def create_active_task(request: ActiveTaskRequest) -> dict:
        result = await capabilities.invoke(
            "task.create",
            {"prompt": request.prompt},
            permission="prepare",
            context=CapabilityContext(
                home=home,
                peer_id=request.peer_id,
                sender_id=request.sender_id,
                source=load_config(home).runtime.local_surface,
            ),
        )
        task = task_store.get(result.task_id) if result.task_id else None
        approval = task_store.pending_approval_for_task(result.task_id, sender_id=request.sender_id) if result.task_id else None
        message = result.message or result.observation
        if task and approval:
            message = (
                f"Task `{task.id}` is prepared for approval.\n"
                f"Preparation:\n{task.plan_summary or '(no preparation output)'}\n\n"
                f"Approval expires in 15 minutes.\n"
                f"Approval code: `{approval.code}`.\n"
                f"Reply with `approve {approval.code}` or `reject {approval.code}`."
            )
        return {"message": message, "task": task.__dict__ if task else None}

    @app.post(api_path("active_approve"))
    async def approve_active_task(request: ActiveApprovalRequest) -> dict:
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
        task = task_store.get(result.task_id) if result.task_id else None
        return {"message": result.message or result.observation, "task": task.__dict__ if task else None}

    @app.post(api_path("active_reject"))
    async def reject_active_task(request: ActiveApprovalRequest) -> dict:
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

    @app.get(api_path("tools"))
    def list_tools() -> dict:
        return {
            "tools": [asdict(spec) for spec in capabilities.list_specs()],
            "sources": capabilities.list_sources(),
        }

    @app.post(api_path("tool_call"))
    async def call_tool(tool_name: str, request: ToolCallRequest) -> dict:
        spec = capabilities.get(tool_name)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"capability not found: {tool_name}")
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

    @app.get(api_path("evolution_events"))
    def evolution_events() -> dict:
        return {"events": [event.__dict__ for event in EvolutionLedger(home).list()]}

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

    @app.get(api_path("index"), response_class=HTMLResponse)
    def index() -> str:
        html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        return html.replace(
            "__NAVI_BOOTSTRAP__",
            json.dumps(
                {
                    "apiPaths": API_PATHS,
                    "connectors": [asdict(adapter.spec) for adapter in connector_adapters],
                    "localSurface": load_config(home).runtime.local_surface,
                },
                ensure_ascii=False,
            ),
        )

    @app.get("/navi.svg", include_in_schema=False)
    def navi_icon() -> Response:
        svg = (Path(__file__).parent / "web" / "navi.svg").read_text(encoding="utf-8")
        return Response(svg, media_type="image/svg+xml")

    return app


def _public_approval(approval) -> dict:
    return {
        "id": approval.id,
        "task_id": approval.task_id,
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
        "task_id": result.task_id,
        "terminal": result.terminal,
        "facts": result.facts or {},
    }
