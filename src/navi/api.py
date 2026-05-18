from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .assistant import ActiveAssistant
from .auth import AuthInspector
from .app_factory import build_runtime
from .config import load_config, write_default_config
from .evolution import EvolutionEngine, EvolutionLedger
from .graph import GraphStore
from .paths import ensure_home
from .tasks import TaskStore
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
    peer_id: str = "web"
    sender_id: str = "web"


class ActiveApprovalRequest(BaseModel):
    code: str
    sender_id: str = "web"


class WatchRequest(BaseModel):
    cron: str
    prompt: str
    peer_id: str = "web"
    sender_id: str = "web"


def create_app(home: Path | None = None) -> FastAPI:
    home = home or ensure_home()
    write_default_config(home)
    runtime = build_runtime(home)
    task_store = TaskStore(home)
    active = ActiveAssistant(home)
    app = FastAPI(title="Navi", version="0.1.0")

    @app.get("/health")
    def health() -> dict:
        config = load_config(home)
        return {
            "ok": True,
            "home": str(home),
            "model_provider": config.model.provider,
            "weixin_enabled": config.weixin.enabled,
        }

    @app.post("/v1/chat")
    async def chat(request: ChatRequest) -> dict:
        reply = await runtime.chat(request.message, session_id=request.session_id)
        return {"session_id": reply.session_id, "message": reply.content}

    @app.get("/v1/sessions")
    def sessions() -> dict:
        return {"sessions": runtime.memory.list_sessions()}

    @app.post("/v1/sessions")
    def create_session(request: SessionRequest) -> dict:
        session_id = runtime.memory.create_session(alias=request.alias)
        return {"session_id": session_id, "alias": request.alias}

    @app.get("/v1/session-aliases")
    def session_aliases() -> dict:
        return {"aliases": [alias.__dict__ for alias in runtime.memory.list_session_aliases()]}

    @app.get("/v1/sessions/{session_id}")
    def session(session_id: str) -> dict:
        return {"messages": [message.__dict__ for message in runtime.memory.get_messages(session_id)]}

    @app.get("/v1/memory")
    def get_memory() -> dict:
        return {"memory": runtime.memory.read_memory()}

    @app.post("/v1/memory")
    def add_memory(request: MemoryRequest) -> dict:
        runtime.memory.append_memory(request.text)
        return {"ok": True}

    @app.get("/v1/skills")
    def skills() -> dict:
        return {"skills": [skill.__dict__ | {"path": str(skill.path)} for skill in runtime.skills.list_skills()]}

    @app.get("/v1/tasks")
    def list_tasks() -> dict:
        return {"tasks": [task.__dict__ for task in task_store.list()]}

    @app.post("/v1/tasks")
    def create_task(request: TaskRequest) -> dict:
        return task_store.create(request.title, prompt=request.prompt or request.title).__dict__

    @app.patch("/v1/tasks/{task_id}")
    def update_task(task_id: str, request: TaskStatusRequest) -> dict:
        task = task_store.update_status(task_id, request.status)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task.__dict__

    @app.get("/v1/approvals")
    def list_approvals() -> dict:
        return {"approvals": [approval.__dict__ for approval in task_store.list_approvals()]}

    @app.get("/v1/watches")
    def list_watches() -> dict:
        return {"watches": [watch.__dict__ for watch in task_store.list_watches()]}

    @app.post("/v1/tasks/{task_id}/approve")
    def approve_task(task_id: str) -> dict:
        task = task_store.update_task(task_id, status="queued")
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task.__dict__

    @app.post("/v1/tasks/process")
    async def process_tasks() -> dict:
        return {"tasks": [task.__dict__ for task in await active.process_queue_once()]}

    @app.post("/v1/active/tasks")
    async def create_active_task(request: ActiveTaskRequest) -> dict:
        result = await active.create_task(
            request.prompt,
            peer_id=request.peer_id,
            sender_id=request.sender_id,
            source="web",
        )
        task = task_store.get(result.task_id) if result.task_id else None
        return {"message": result.text, "task": task.__dict__ if task else None}

    @app.post("/v1/active/approve")
    def approve_active_task(request: ActiveApprovalRequest) -> dict:
        result = active.approve(request.code, sender_id=request.sender_id)
        task = task_store.get(result.task_id) if result.task_id else None
        return {"message": result.text, "task": task.__dict__ if task else None}

    @app.post("/v1/active/reject")
    def reject_active_task(request: ActiveApprovalRequest) -> dict:
        result = active.reject(request.code, sender_id=request.sender_id)
        return {"message": result.text}

    @app.post("/v1/active/watches")
    def create_active_watch(request: WatchRequest) -> dict:
        result = active.create_watch(
            f"{request.cron} {request.prompt}",
            peer_id=request.peer_id,
            sender_id=request.sender_id,
        )
        return {"message": result.text}

    @app.post("/v1/active/watches/process")
    async def process_watches() -> dict:
        return {"results": [result.__dict__ for result in await active.process_watches_once()]}

    @app.get("/v1/auth/status")
    def auth_status() -> dict:
        return {"providers": [item.__dict__ for item in AuthInspector().status()]}

    @app.get("/v1/graph")
    def graph() -> dict:
        return {"nodes": [node.__dict__ for node in GraphStore(home).list()]}

    @app.get("/v1/trust-rules")
    def trust_rules() -> dict:
        return {"trust_rules": [rule.__dict__ for rule in TrustStore(home).list()]}

    @app.get("/v1/evolution-events")
    def evolution_events() -> dict:
        return {"events": [event.__dict__ for event in EvolutionLedger(home).list()]}

    @app.post("/v1/evolution-events/{event_id}/rollback")
    def rollback_evolution(event_id: str) -> dict:
        event = EvolutionEngine(home).rollback(event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="event not found")
        return event.__dict__

    @app.get("/v1/weixin/status")
    def weixin_status() -> dict:
        return WeixinService(home=home, config=load_config(home).weixin, runtime=runtime).status()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")

    return app
