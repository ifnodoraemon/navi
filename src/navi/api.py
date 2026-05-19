from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .api_paths import API_PATHS, api_path
from .assistant import ActiveAssistant
from .auth import AuthInspector
from .app_factory import build_runtime
from .config import load_config, write_default_config
from .connector_specs import list_connector_specs
from .defaults import DEFAULT_LOCAL_SURFACE
from .evolution import EvolutionEngine, EvolutionLedger
from .graph import GraphStore
from .paths import ensure_home
from .tasks import TaskStore
from .tools import build_core_tool_registry
from .trust import TrustStore
from .weixin.service import WeixinService


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
    active = ActiveAssistant(home)
    tools = build_core_tool_registry(home, project_dir=Path.cwd())
    connector_status_handlers = {
        WeixinService.connector.name: lambda: WeixinService(
            home=home,
            config=load_config(home).weixin,
            runtime=runtime,
        ).status()
    }
    app = FastAPI(title="Navi", version="0.1.0")

    @app.get(api_path("health"))
    def health() -> dict:
        config = load_config(home)
        return {
            "ok": True,
            "home": str(home),
            "model_provider": config.model.provider,
            "connectors": {
                WeixinService.connector.name: {
                    "enabled": config.weixin.enabled,
                },
            },
        }

    @app.post(api_path("chat"))
    async def chat(request: ChatRequest) -> dict:
        reply = await runtime.chat(request.message, session_id=request.session_id)
        return {"session_id": reply.session_id, "message": reply.content}

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
    def create_task(request: TaskRequest) -> dict:
        return task_store.create(request.title, prompt=request.prompt or request.title).__dict__

    @app.patch(api_path("task"))
    def update_task(task_id: str, request: TaskStatusRequest) -> dict:
        task = task_store.update_status(task_id, request.status)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task.__dict__

    @app.get(api_path("approvals"))
    def list_approvals() -> dict:
        return {"approvals": [approval.__dict__ for approval in task_store.list_approvals()]}

    @app.get(api_path("watches"))
    def list_watches() -> dict:
        return {"watches": [watch.__dict__ for watch in task_store.list_watches()]}

    @app.post(api_path("task_approve"))
    def approve_task(task_id: str) -> dict:
        task = task_store.update_task(task_id, status="queued")
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task.__dict__

    @app.post(api_path("tasks_process"))
    async def process_tasks() -> dict:
        return {"tasks": [task.__dict__ for task in await active.process_queue_once()]}

    @app.post(api_path("active_tasks"))
    async def create_active_task(request: ActiveTaskRequest) -> dict:
        result = await active.create_task(
            request.prompt,
            peer_id=request.peer_id,
            sender_id=request.sender_id,
            source=load_config(home).runtime.local_surface,
        )
        task = task_store.get(result.task_id) if result.task_id else None
        return {"message": result.text, "task": task.__dict__ if task else None}

    @app.post(api_path("active_approve"))
    def approve_active_task(request: ActiveApprovalRequest) -> dict:
        result = active.approve(request.code, sender_id=request.sender_id)
        task = task_store.get(result.task_id) if result.task_id else None
        return {"message": result.text, "task": task.__dict__ if task else None}

    @app.post(api_path("active_reject"))
    def reject_active_task(request: ActiveApprovalRequest) -> dict:
        result = active.reject(request.code, sender_id=request.sender_id)
        return {"message": result.text}

    @app.post(api_path("active_watches"))
    def create_active_watch(request: WatchRequest) -> dict:
        result = active.create_watch(
            f"{request.cron} {request.prompt}",
            peer_id=request.peer_id,
            sender_id=request.sender_id,
        )
        return {"message": result.text}

    @app.post(api_path("active_watches_process"))
    async def process_watches() -> dict:
        return {"results": [result.__dict__ for result in await active.process_watches_once()]}

    @app.get(api_path("auth_status"))
    def auth_status() -> dict:
        return {"providers": [item.__dict__ for item in AuthInspector().status()]}

    @app.get(api_path("tools"))
    def list_tools() -> dict:
        return {"tools": [asdict(spec) for spec in tools.list_specs()]}

    @app.post(api_path("tool_call"))
    def call_tool(tool_name: str, request: ToolCallRequest) -> dict:
        result = tools.call(tool_name, request.args)
        if not result.ok and not tools.get(tool_name):
            raise HTTPException(status_code=404, detail=result.error)
        return result.to_dict()

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
                    "connectors": [asdict(spec) for spec in list_connector_specs()],
                    "localSurface": load_config(home).runtime.local_surface,
                },
                ensure_ascii=False,
            ),
        )

    return app
