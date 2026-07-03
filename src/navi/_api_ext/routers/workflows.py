from typing import Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...api_paths import api_path
from ...approval_contract import APPROVAL_DECISION_APPROVE, APPROVAL_DECISION_REJECT
from ...workflows import workflow_facts
from ..utils import local_capability_context, raise_capability_error

class WorkflowRequest(BaseModel):
    objective: str
    permission_ceiling: str = "read"
    max_concurrency: int = 4
    total_subagent_limit: int = 32
    risk_class: str = ""
    estimated_cost: str = ""
    stop_condition: str = ""
    verification_strategy: str = ""
    plan: dict[str, Any] = Field(default_factory=dict)
    steps: list[dict[str, Any]] = Field(default_factory=list)

def create_router(home, project_dir, capabilities, workflow_store):
    router = APIRouter()

    @router.get(api_path("workflows"))
    def list_workflows(status: str = "", limit: int = 50) -> dict:
        return {
            "workflows": [
                workflow.__dict__ for workflow in workflow_store.list(status=status, limit=limit)
            ]
        }

    @router.post(api_path("workflows"))
    async def create_workflow(request: WorkflowRequest) -> dict:
        result = await capabilities.invoke(
            "workflow.propose",
            request.model_dump(),
            permission="prepare",
            context=local_capability_context(home, project_dir=project_dir),
        )
        raise_capability_error(result)
        workflow_id = str((result.facts or {}).get("workflow_id") or result.run_id)
        workflow = workflow_store.get(workflow_id)
        if workflow is None:
            raise HTTPException(
                status_code=500, detail="workflow.propose did not create a workflow"
            )
        return workflow_facts(workflow_store, workflow)

    @router.get(api_path("workflow"))
    def get_workflow(workflow_id: str) -> dict:
        workflow = workflow_store.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        return workflow_facts(workflow_store, workflow)

    @router.post(api_path("workflow_approve"))
    async def approve_workflow(workflow_id: str) -> dict:
        return await _workflow_action(
            "workflow.approve", workflow_id, {"decision": APPROVAL_DECISION_APPROVE}
        )

    @router.post(api_path("workflow_reject"))
    async def reject_workflow(workflow_id: str) -> dict:
        return await _workflow_action(
            "workflow.approve", workflow_id, {"decision": APPROVAL_DECISION_REJECT}
        )

    @router.post(api_path("workflow_run"))
    async def run_workflow(workflow_id: str, resume: bool = False) -> dict:
        return await _workflow_action("workflow.run", workflow_id, {"resume": resume})

    async def _workflow_action(
        tool: str, workflow_id: str, extra_args: dict[str, Any] | None = None
    ) -> dict:
        result = await capabilities.invoke(
            tool,
            {"workflow_id": workflow_id, **(extra_args or {})},
            permission="write",
            context=local_capability_context(home, project_dir=project_dir),
        )
        raise_capability_error(result, not_found_status=404)
        workflow = workflow_store.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        return workflow_facts(workflow_store, workflow)

    return router
