from fastapi import APIRouter, HTTPException

from ...api_paths import api_path

def create_router(goal_store, subagent_store):
    router = APIRouter()

    @router.get(api_path("goals"))
    def list_goals(phase: str = "", limit: int = 50) -> dict:
        return {"goals": [goal.__dict__ for goal in goal_store.list(phase=phase, limit=limit)]}

    @router.get(api_path("goal"))
    def get_goal(goal_id: str) -> dict:
        goal = goal_store.get(goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="goal not found")
        return {
            "goal": goal.__dict__,
            "events": [event.__dict__ for event in goal_store.list_events(goal_id)],
        }

    @router.get(api_path("subagents"))
    def list_subagents(role: str = "", status: str = "", run_id: str = "", limit: int = 50) -> dict:
        return {
            "subagents": [
                subagent.__dict__
                for subagent in subagent_store.list(
                    role=role, status=status, run_id=run_id, limit=limit
                )
            ]
        }

    @router.get(api_path("subagent"))
    def get_subagent(subagent_id: str) -> dict:
        subagent = subagent_store.get(subagent_id)
        if subagent is None:
            raise HTTPException(status_code=404, detail="subagent run not found")
        return {"subagent": subagent.__dict__}

    return router
