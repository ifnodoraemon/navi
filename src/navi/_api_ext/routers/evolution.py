from fastapi import APIRouter
from pydantic import BaseModel, Field

from ...api_paths import api_path
from ...evolution import EvolutionLedger, list_evolution_targets
from ..utils import local_capability_context, raise_capability_error

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
    evaluation_evidence: str = ""

def create_router(home, project_dir, api_capabilities):
    router = APIRouter()

    @router.get(api_path("evolution_events"))
    def evolution_events() -> dict:
        return {"events": [event.__dict__ for event in EvolutionLedger(home).list()]}

    @router.get(api_path("evolution_targets"))
    def evolution_targets() -> dict:
        return {"targets": list_evolution_targets()}

    @router.get(api_path("evolution_proposals"))
    def evolution_proposals(status: str | None = None) -> dict:
        return {
            "proposals": [
                proposal.__dict__
                for proposal in EvolutionLedger(home).list_proposals(status=status)
            ]
        }

    @router.post(api_path("evolution_proposals"))
    async def create_evolution_proposal(request: EvolutionProposalRequest) -> dict:
        result = await api_capabilities.invoke(
            "evolution.propose",
            request.model_dump(),
            permission="prepare",
            context=local_capability_context(home, project_dir=project_dir),
        )
        raise_capability_error(result)
        return (result.facts or {}).get("proposal", {})

    @router.post(api_path("evolution_proposal_apply"))
    async def apply_evolution_proposal(proposal_id: str) -> dict:
        result = await api_capabilities.invoke(
            "evolution.apply",
            {"proposal_id": proposal_id},
            permission="write",
            context=local_capability_context(home, project_dir=project_dir),
        )
        raise_capability_error(result, not_found_status=404)
        return (result.facts or {}).get("event", {})

    @router.post(api_path("evolution_proposal_evaluation"))
    async def record_evolution_proposal_evaluation(
        proposal_id: str, request: EvolutionEvaluationRequest
    ) -> dict:
        result = await api_capabilities.invoke(
            "evolution.record_evaluation",
            {
                "proposal_id": proposal_id,
                "evaluation_result": request.evaluation_result,
                "evaluation_evidence": request.evaluation_evidence,
            },
            permission="write",
            context=local_capability_context(home, project_dir=project_dir),
        )
        raise_capability_error(result, not_found_status=404)
        return (result.facts or {}).get("proposal", {})

    @router.post(api_path("evolution_rollback"))
    async def rollback_evolution(event_id: str) -> dict:
        result = await api_capabilities.invoke(
            "evolution.rollback",
            {"event_id": event_id},
            permission="write",
            context=local_capability_context(home, project_dir=project_dir),
        )
        raise_capability_error(result, not_found_status=404)
        return (result.facts or {}).get("event", {})

    return router
