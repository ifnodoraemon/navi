"""E2E tests for Feature 3 (API Boundary Standardization)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from navi.evolution_targets import EvolutionTargetAdapterRegistry
from navi.prompting import PromptLayerStore
from navi.runs import RunStore

if TYPE_CHECKING:
    from fastapi.testclient import TestClient
    from pathlib import Path


def _logged_tools(home: Path) -> set[str]:
    return {log.tool for log in RunStore(home).list_tool_call_logs(limit=100)}


def _prompt_proposal(home: Path, *, marker: str, eval_case_id: str) -> dict:
    before = PromptLayerStore(home).read("planner")
    EvolutionTargetAdapterRegistry(home).get("eval_case").apply(
        eval_case_id,
        json.dumps(
            {
                "id": eval_case_id,
                "target_types": ["prompt_layer"],
                "assertions": [{"type": "contains", "value": marker}],
            },
            sort_keys=True,
        ),
    )
    return {
        "target_type": "prompt_layer",
        "target_id": "planner",
        "reason": f"E2E governed evolution: {marker}",
        "expected_benefit": "verify the governed API lifecycle",
        "risk": "behavior change",
        "before": before,
        "after": before + f"\n{marker}\n",
        "rollback_plan": "restore the exact prompt snapshot",
        "eval_cases": [eval_case_id],
    }


def _approve_required_response(api_client: TestClient, response) -> str:
    assert response.status_code == 409
    approval = response.json()["error"]["detail"]["approval"]
    approved = api_client.post(
        "/v1/active/approve",
        json={"code": approval["code"]},
    )
    assert approved.status_code == 200
    assert approved.json()["data"]["facts"]["decision"] == "approve"
    return str(approval["id"])


def test_t1_api_create_session_routes_capability(
    api_client: TestClient, navi_home: Path
) -> None:
    """Execute a POST /v1/sessions request via api_client and verify it creates the session."""
    alias = "e2e-session-alias"
    response = api_client.post("/v1/sessions", json={"alias": alias})
    assert response.status_code == 200
    
    data = response.json()["data"]
    assert "session_id" in data
    assert data["alias"] == alias
    session_id = data["session_id"]
    assert session_id

    # Verify session alias is created by checking the session-aliases endpoint
    get_response = api_client.get("/v1/session-aliases")
    assert get_response.status_code == 200
    aliases = get_response.json()["data"].get("aliases", [])
    aliases_list = [a["alias"] for a in aliases]
    assert alias in aliases_list
    assert "session.create" in _logged_tools(navi_home)


def test_t1_api_add_memory_routes_capability(
    api_client: TestClient, navi_home: Path
) -> None:
    """Execute a POST /v1/memory request via api_client and verify it adds the memory item."""
    memory_data = {
        "type": "preference",
        "content": "E2E boundary test content",
        "source": "api-boundary-test",
        "status": "active",
        "confidence": 0.9,
        "reason": "E2E validates API memory boundary",
        "provenance": "tests_e2e:t1_api_boundary",
    }
    response = api_client.post("/v1/memory", json=memory_data)
    assert response.status_code == 200
    
    data = response.json()["data"]
    assert "item" in data
    item = data["item"]
    assert item["type"] == "preference"
    assert item["content"] == "E2E boundary test content"
    assert item["source"] == "api-boundary-test"
    assert item["status"] == "active"
    assert item["confidence"] == 0.9
    assert item["reason"] == "E2E validates API memory boundary"
    assert item["provenance"] == "tests_e2e:t1_api_boundary"

    # Verify database mutation by fetching memory items
    get_response = api_client.get("/v1/memory", params={"status": "active"})
    assert get_response.status_code == 200
    items = get_response.json()["data"].get("items", [])
    item_contents = {it["content"] for it in items}
    assert "E2E boundary test content" in item_contents
    assert "memory.add" in _logged_tools(navi_home)


def test_t1_api_trace_evaluate_routes_capability(api_client: TestClient, navi_home: Path) -> None:
    """Execute a POST /v1/trace_evaluate request via api_client and verify it returns trace evaluation details."""
    from navi.trace import TraceStore

    trace_id = "boundary-trace-id"
    # Seed a trace event directly in the database/store
    TraceStore(navi_home).add_event(
        trace_id=trace_id,
        phase="capability.result",
        tool="delegate.spawn",
        ok=False,
        message="boundary check failure",
    )

    # Trigger evaluation via API client
    response = api_client.post(f"/v1/traces/{trace_id}/evaluate")
    assert response.status_code == 200

    data = response.json()["data"]
    assert "id" in data
    assert data["trace_id"] == trace_id
    assert data["failure_domain"] == "capability_failure"
    
    pass
    assert "trace.evaluate" in _logged_tools(navi_home)


def test_t1_api_evolution_propose_routes_capability(
    api_client: TestClient, navi_home: Path
) -> None:
    """Execute a POST /v1/evolution_proposals request via api_client and verify it proposes an evolution."""
    proposal_data = _prompt_proposal(
        navi_home,
        marker="api boundary proposal marker",
        eval_case_id="verify-api-endpoint",
    )
    response = api_client.post("/v1/evolution-proposals", json=proposal_data)
    assert response.status_code == 200

    data = response.json()["data"]
    assert "id" in data
    assert data["target_type"] == "prompt_layer"
    assert data["target_id"] == "planner"
    assert data["reason"] == proposal_data["reason"]
    assert data["status"] == "proposed"

    # Verify mutation via GET /v1/evolution-proposals
    get_response = api_client.get("/v1/evolution-proposals", params={"status": "proposed"})
    assert get_response.status_code == 200
    proposals = get_response.json()["data"].get("proposals", [])
    proposal_ids = {prop["id"] for prop in proposals}
    assert data["id"] in proposal_ids
    assert "evolution.propose" in _logged_tools(navi_home)


def test_t1_api_evolution_apply_routes_capability(
    api_client: TestClient, navi_home: Path
) -> None:
    """Execute a POST /v1/evolution_proposal_apply request via api_client and verify it applies the proposal."""
    # 1. Propose an evolution
    proposal_data = _prompt_proposal(
        navi_home,
        marker="api apply marker",
        eval_case_id="api-apply-case",
    )
    response = api_client.post("/v1/evolution-proposals", json=proposal_data)
    assert response.status_code == 200
    proposal_id = response.json()["data"]["id"]

    # 2. Persist candidate experiment evidence.
    experiment = api_client.post(f"/v1/evolution-proposals/{proposal_id}/experiment")
    assert experiment.status_code == 200
    assert experiment.json()["data"]["status"] == "passed"

    # 3. Request and resolve an exact apply approval.
    first_apply = api_client.post(f"/v1/evolution-proposals/{proposal_id}/apply")
    approval_id = _approve_required_response(api_client, first_apply)

    # 4. Bind the approved evaluation to that durable approval.
    eval_response = api_client.post(
        f"/v1/evolution-proposals/{proposal_id}/evaluation",
        json={
            "evaluation_result": "approved",
            "evaluation_evidence": "E2E apply checks passed",
            "approval_id": approval_id,
        },
    )
    assert eval_response.status_code == 200
    assert eval_response.json()["data"]["evaluation_result"] == "approved"

    # 5. Apply the proposal after both experiment and approval are durable.
    apply_response = api_client.post(f"/v1/evolution-proposals/{proposal_id}/apply")
    assert apply_response.status_code == 200

    apply_data = apply_response.json()["data"]
    assert "id" in apply_data
    assert apply_data["target_type"] == "prompt_layer"
    assert apply_data["target_id"] == "planner"
    assert apply_data["reason"] == proposal_data["reason"]
    logged = _logged_tools(navi_home)
    assert {"evolution.propose", "evolution.record_evaluation", "evolution.apply"} <= logged


def test_t1_api_evolution_rollback_routes_capability(
    api_client: TestClient, navi_home: Path
) -> None:
    """Execute a POST /v1/evolution_rollback request via api_client and verify it rolls back the evolution event."""
    # 1. Propose an evolution
    proposal_data = _prompt_proposal(
        navi_home,
        marker="api rollback marker",
        eval_case_id="api-rollback-case",
    )
    response = api_client.post("/v1/evolution-proposals", json=proposal_data)
    assert response.status_code == 200
    proposal_id = response.json()["data"]["id"]

    # 2. Run the declared experiment and request exact apply approval.
    experiment = api_client.post(f"/v1/evolution-proposals/{proposal_id}/experiment")
    assert experiment.status_code == 200
    first_apply = api_client.post(f"/v1/evolution-proposals/{proposal_id}/apply")
    approval_id = _approve_required_response(api_client, first_apply)

    # 3. Bind the proposal evaluation to the approved apply request.
    eval_response = api_client.post(
        f"/v1/evolution-proposals/{proposal_id}/evaluation",
        json={
            "evaluation_result": "approved",
            "evaluation_evidence": "E2E rollback checks passed",
            "approval_id": approval_id,
        },
    )
    assert eval_response.status_code == 200

    # 4. Apply proposal (generates an evolution event).
    apply_response = api_client.post(f"/v1/evolution-proposals/{proposal_id}/apply")
    assert apply_response.status_code == 200
    event_id = apply_response.json()["data"]["id"]

    # 5. Rollback has its own exact approval gate.
    first_rollback = api_client.post(f"/v1/evolution-events/{event_id}/rollback")
    _approve_required_response(api_client, first_rollback)
    rollback_response = api_client.post(f"/v1/evolution-events/{event_id}/rollback")
    assert rollback_response.status_code == 200

    rollback_data = rollback_response.json()["data"]
    assert rollback_data["id"] == event_id
    assert rollback_data["rolled_back_at"] > 0
    assert "evolution.rollback" in _logged_tools(navi_home)
