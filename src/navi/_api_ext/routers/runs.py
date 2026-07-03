from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...api_paths import api_path
from ...approval_contract import APPROVAL_DECISION_APPROVE, APPROVAL_DECISION_REJECT
from ...capabilities import CapabilityContext
from ...config import load_config
from ...defaults import DEFAULT_LOCAL_SURFACE
from ..utils import local_capability_context, raise_capability_error

class DelegationRequest(BaseModel):
    title: str
    prompt: str | None = None
    context: str | None = None
    plan: str | None = None
    success_criteria: str | None = None

class DelegationStatusRequest(BaseModel):
    status: str

class ActiveDelegationRequest(BaseModel):
    prompt: str
    peer_id: str = DEFAULT_LOCAL_SURFACE
    sender_id: str = DEFAULT_LOCAL_SURFACE
    context: str | None = None
    plan: str | None = None
    success_criteria: str | None = None

class ActiveApprovalRequest(BaseModel):
    code: str
    sender_id: str = DEFAULT_LOCAL_SURFACE

class WatchRequest(BaseModel):
    cron: str
    prompt: str
    peer_id: str = DEFAULT_LOCAL_SURFACE
    sender_id: str = DEFAULT_LOCAL_SURFACE

def _delegation_spawn_args(*, objective: str, context: str | None, plan: str | None, success_criteria: str | None) -> dict[str, str]:
    return {
        "objective": objective,
        "context": context or "API request context was not provided.",
        "plan": plan or "API request execution plan was not provided.",
        "success_criteria": success_criteria or "API request success criteria were not provided.",
    }

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

def _local_result_message(result, *, source: str) -> str:
    del source
    return result.message

def create_router(home, project_dir, daemon, capabilities, api_capabilities, task_store):
    router = APIRouter()

    @router.get(api_path("delegations"))
    def list_delegations() -> dict:
        return {"delegations": [task.__dict__ for task in task_store.list()]}

    @router.post(api_path("delegations"))
    async def create_delegation(request: DelegationRequest) -> dict:
        result = await capabilities.invoke(
            "delegate.spawn",
            _delegation_spawn_args(
                objective=request.prompt or request.title,
                context=request.context,
                plan=request.plan,
                success_criteria=request.success_criteria,
            ),
            permission="prepare",
            context=local_capability_context(home, project_dir=project_dir),
        )
        raise_capability_error(result)
        prepared = await capabilities.invoke(
            "delegate.prepare",
            {"run_id": result.run_id},
            permission="prepare",
            context=local_capability_context(home, project_dir=project_dir),
        )
        raise_capability_error(prepared)
        requested = await capabilities.invoke(
            "approval.request",
            {"run_id": result.run_id},
            permission="prepare",
            context=local_capability_context(home, project_dir=project_dir),
        )
        raise_capability_error(requested)
        task = task_store.get(result.run_id) if result.run_id else None
        if task is None:
            raise HTTPException(
                status_code=500, detail="delegate.spawn did not return a delegation run"
            )
        return task.__dict__

    @router.patch(api_path("delegation"))
    async def update_delegation(run_id: str, request: DelegationStatusRequest) -> dict:
        decision_by_status = {
            "queued": APPROVAL_DECISION_APPROVE,
            "rejected": APPROVAL_DECISION_REJECT,
        }
        decision = decision_by_status.get(request.status)
        if decision is None:
            raise HTTPException(
                status_code=409,
                detail="delegation status transitions must go through delegation capabilities",
            )
        result = await api_capabilities.invoke(
            "approval.resolve",
            {"decision": decision, "run_id": run_id},
            permission="write",
            context=local_capability_context(home, project_dir=project_dir),
        )
        raise_capability_error(result)
        task = task_store.get(run_id)
        if task is None:
            raise HTTPException(status_code=404, detail="delegation run not found")
        return task.__dict__

    @router.delete(api_path("delegation"))
    async def delete_delegation(run_id: str) -> dict:
        result = await capabilities.invoke(
            "delegate.delete",
            {"run_id": run_id, "reason": "api delegation delete request"},
            permission="write",
            context=local_capability_context(home, project_dir=project_dir),
        )
        raise_capability_error(result, not_found_status=404)
        return {"deleted": True, "delegation": result.facts}

    @router.get(api_path("approvals"))
    def list_approvals() -> dict:
        return {
            "approvals": [_public_approval(approval) for approval in task_store.list_approvals()]
        }

    @router.get(api_path("watches"))
    def list_watches() -> dict:
        return {"watches": [watch.__dict__ for watch in task_store.list_watches()]}

    @router.post(api_path("delegation_approve"))
    async def approve_delegation(run_id: str) -> dict:
        result = await api_capabilities.invoke(
            "approval.resolve",
            {"decision": APPROVAL_DECISION_APPROVE, "run_id": run_id},
            permission="write",
            context=local_capability_context(home, project_dir=project_dir),
        )
        raise_capability_error(result, not_found_status=409)
        task = task_store.get(run_id)
        if task is None:
            raise HTTPException(status_code=404, detail="delegation run not found")
        return task.__dict__

    @router.post(api_path("delegations_process"))
    async def process_delegations() -> dict:
        return {"delegations": [task.__dict__ for task in await daemon.process_queue_once()]}

    @router.post(api_path("active_delegations"))
    async def create_active_delegation(request: ActiveDelegationRequest) -> dict:
        context = CapabilityContext(
            home=home,
            peer_id=request.peer_id,
            sender_id=request.sender_id,
            source=load_config(home).runtime.local_surface,
            permission_ceiling="write",
            workspace=str(project_dir),
        )
        result = await capabilities.invoke(
            "delegate.spawn",
            _delegation_spawn_args(
                objective=request.prompt,
                context=request.context,
                plan=request.plan,
                success_criteria=request.success_criteria,
            ),
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
        source = load_config(home).runtime.local_surface
        return {
            "message": _local_result_message(result, source=source),
            "delegation": task.__dict__ if task else None,
            "preparation": task.plan_summary if task else "",
            "facts": result.facts or {},
        }

    @router.post(api_path("active_approve"))
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

    @router.post(api_path("active_reject"))
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

    @router.post(api_path("active_watches"))
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
                permission_ceiling="write",
                workspace=str(project_dir),
            ),
        )
        watch_id = str((result.facts or {}).get("watch_id") or "")
        watch = task_store.get_watch(watch_id) if watch_id else None
        return {
            "message": _local_result_message(
                result, source=load_config(home).runtime.local_surface
            ),
            "watch": watch.__dict__ if watch else None,
            "facts": result.facts or {},
        }

    @router.post(api_path("active_watches_process"))
    async def process_watches() -> dict:
        return {"results": await daemon.process_watches_once()}

    return router
