from fastapi import APIRouter

from ...api_paths import api_path
from ...graph import GraphStore
from ...json_utils import json_object
from ...trace import TraceStore
from ..utils import local_capability_context, raise_capability_error

def create_router(home, project_dir, api_capabilities):
    router = APIRouter()

    @router.get(api_path("graph"))
    def graph() -> dict:
        return {"nodes": [node.__dict__ for node in GraphStore(home).list()]}

    @router.get(api_path("traces"))
    def traces(limit: int = 50, offset: int = 0, has_error: bool | None = None, query: str = "") -> dict:
        return {"traces": TraceStore(home).list_trace_meta(limit=limit, offset=offset, has_error=has_error, query=query)}

    @router.delete(api_path("traces"))
    def delete_all_traces() -> dict:
        TraceStore(home).delete_traces()
        return {"status": "ok"}

    @router.delete(api_path("traces") + "/{trace_id}")
    def delete_trace(trace_id: str) -> dict:
        TraceStore(home).delete_traces(trace_id)
        return {"status": "ok"}

    @router.get(api_path("trace"))
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

    @router.get(api_path("trace_decisions"))
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

    @router.get(api_path("trace_runs"))
    def trace_runs(trace_id: str) -> dict:
        store = TraceStore(home)
        return {
            "runs": [run.to_dict() for run in store.list_run_views(trace_id)],
            "loop_runs": store.list_loop_run_details(trace_id),
        }

    @router.get(api_path("trace_evaluations"))
    def trace_evaluations(trace_id: str = "", limit: int = 50) -> dict:
        return {
            "evaluations": [
                item.to_dict() for item in TraceStore(home).list_evaluations(trace_id, limit=limit)
            ]
        }

    @router.post(api_path("trace_evaluate"))
    async def trace_evaluate(trace_id: str, session_id: str = "") -> dict:
        result = await api_capabilities.invoke(
            "trace.evaluate",
            {"trace_id": trace_id, "session_id": session_id},
            permission="write",
            context=local_capability_context(home, project_dir=project_dir),
        )
        raise_capability_error(result)
        return (result.facts or {}).get("evaluation", {})

    return router
