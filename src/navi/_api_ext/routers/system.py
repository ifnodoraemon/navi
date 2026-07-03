from dataclasses import asdict
from typing import Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...api_paths import api_path
from ...auth import AuthInspector
from ...config import load_config
from ...diagnostics import run_diagnostics
from ..utils import local_capability_context, raise_capability_error

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

class ToolCallRequest(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)

def _capability_result_dict(result) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "action": result.action,
        "observation": result.observation,
        "message": result.message,
        "run_id": result.run_id,
        "terminal": result.terminal,
        "facts": result.facts or {},
    }

def create_router(home, project_dir, runtime, agent, capabilities, api_capabilities, connector_adapters, connector_status_handlers):
    router = APIRouter()

    @router.get(api_path("health"))
    def health() -> dict:
        config = load_config(home)
        return {
            "ok": True,
            "home": str(home),
            "model_provider": config.model.provider,
            "connectors": {
                adapter.name: {"enabled": adapter.enabled(home)} for adapter in connector_adapters
            },
        }

    @router.post(api_path("chat"))
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

    @router.get(api_path("sessions"))
    def sessions() -> dict:
        return {"sessions": runtime.memory.list_sessions()}

    @router.post(api_path("sessions"))
    async def create_session(request: SessionRequest) -> dict:
        result = await api_capabilities.invoke(
            "session.create",
            request.model_dump(exclude_none=True),
            permission="prepare",
            context=local_capability_context(home, project_dir=project_dir),
        )
        raise_capability_error(result)
        facts = result.facts or {}
        return {"session_id": facts.get("session_id", ""), "alias": facts.get("alias", "")}

    @router.get(api_path("session_aliases"))
    def session_aliases() -> dict:
        return {"aliases": [alias.__dict__ for alias in runtime.memory.list_session_aliases()]}

    @router.get(api_path("session"))
    def session(session_id: str) -> dict:
        return {
            "messages": [message.__dict__ for message in runtime.memory.get_messages(session_id)]
        }

    @router.get(api_path("memory"))
    def get_memory(
        memory_type: str | None = None, status: str | None = None, limit: int = 50
    ) -> dict:
        items = runtime.memory.list_items(memory_type=memory_type, status=status, limit=limit)
        return {"items": [asdict(item) for item in items]}

    @router.get(api_path("memory_conflicts"))
    def get_memory_conflicts(limit: int = 50) -> dict:
        conflicts = runtime.memory.list_conflicts(limit=limit)
        return {
            "conflicts": [asdict(conflict) for conflict in conflicts],
            "count": len(conflicts),
            "unresolved_count": len(
                [conflict for conflict in conflicts if conflict.status == "unresolved"]
            ),
        }

    @router.post(api_path("memory"))
    async def add_memory(request: MemoryRequest) -> dict:
        result = await api_capabilities.invoke(
            "memory.add",
            request.model_dump(by_alias=True),
            permission="write",
            context=local_capability_context(home, project_dir=project_dir),
        )
        raise_capability_error(result)
        return {"item": (result.facts or {}).get("item", {})}

    @router.get(api_path("skills"))
    def skills() -> dict:
        return {
            "skills": [
                skill.__dict__ | {"path": str(skill.path)} for skill in runtime.skills.list_skills()
            ]
        }

    @router.get(api_path("auth_status"))
    def auth_status() -> dict:
        return {"providers": [item.__dict__ for item in AuthInspector().status()]}

    @router.get(api_path("diagnostics"))
    def diagnostics(connectivity: bool = False) -> dict:
        return {
            "checks": [
                check.__dict__
                for check in run_diagnostics(
                    home, project_dir=project_dir, include_connectivity=connectivity
                )
            ]
        }

    @router.get(api_path("tools"))
    def list_tools() -> dict:
        return {
            "tools": [asdict(spec) for spec in capabilities.list_specs()],
            "capabilities": [asdict(node) for node in capabilities.capability_graph()],
            "sources": capabilities.list_sources(),
        }

    @router.post(api_path("tool_call"))
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
            context=local_capability_context(home, project_dir=project_dir),
        )
        return _capability_result_dict(result)

    @router.get(api_path("connector_status"))
    def connector_status(connector_name: str) -> dict:
        handler = connector_status_handlers.get(connector_name)
        if handler is None:
            raise HTTPException(status_code=404, detail="connector not found")
        return handler()

    return router
