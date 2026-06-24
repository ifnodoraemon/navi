"""E2E tests for Feature 3 (API Boundary Standardization)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from navi.runs import RunStore

if TYPE_CHECKING:
    from fastapi.testclient import TestClient
    from pathlib import Path


def _logged_tools(home: Path) -> set[str]:
    return {log.tool for log in RunStore(home).list_tool_call_logs(limit=100)}


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
        tool="delegate.run",
        ok=False,
        message="boundary check failure",
    )

    # Trigger evaluation via API client
    response = api_client.post(f"/v1/traces/{trace_id}/evaluate")
    assert response.status_code == 200

    data = response.json()["data"]
    assert "id" in data
    assert data["trace_id"] == trace_id
    assert data["failure_domain"] == "tool_or_capability"
    assert data["diagnostic"] == "first failed event was a capability result without safeguard decision facts"
    
    evidence = json.loads(data["evidence_json"])
    assert evidence.get("first_failure_message") == "boundary check failure"
    assert "trace.evaluate" in _logged_tools(navi_home)


def test_t1_api_evolution_propose_routes_capability(
    api_client: TestClient, navi_home: Path
) -> None:
    """Execute a POST /v1/evolution_proposals request via api_client and verify it proposes an evolution."""
    proposal_data = {
        "target_type": "memory_schema",
        "target_id": "api_boundary_policy",
        "reason": "E2E evolution proposal test",
        "expected_benefit": "improved validation",
        "risk": "low",
        "before": "old schema",
        "after": "new schema",
        "rollback_plan": "revert changes",
        "eval_cases": ["verify_api_endpoint"],
    }
    response = api_client.post("/v1/evolution-proposals", json=proposal_data)
    assert response.status_code == 200

    data = response.json()["data"]
    assert "id" in data
    assert data["target_type"] == "memory_schema"
    assert data["target_id"] == "api_boundary_policy"
    assert data["reason"] == "E2E evolution proposal test"
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
    proposal_data = {
        "target_type": "memory_schema",
        "target_id": "apply_policy",
        "reason": "E2E evolution apply test",
        "expected_benefit": "apply test benefit",
        "risk": "low",
        "before": "before apply",
        "after": "after apply",
        "rollback_plan": "rollback apply",
        "eval_cases": ["apply_case"],
    }
    response = api_client.post("/v1/evolution-proposals", json=proposal_data)
    assert response.status_code == 200
    proposal_id = response.json()["data"]["id"]

    # 2. Record proposal evaluation as approved
    eval_response = api_client.post(
        f"/v1/evolution-proposals/{proposal_id}/evaluation",
        json={"evaluation_result": "approved"},
    )
    assert eval_response.status_code == 200
    assert eval_response.json()["data"]["evaluation_result"] == "approved"

    # 3. Apply the proposal
    apply_response = api_client.post(f"/v1/evolution-proposals/{proposal_id}/apply")
    assert apply_response.status_code == 200

    apply_data = apply_response.json()["data"]
    assert "id" in apply_data
    assert apply_data["target_type"] == "memory_schema"
    assert apply_data["target_id"] == "apply_policy"
    assert apply_data["reason"] == "E2E evolution apply test"
    logged = _logged_tools(navi_home)
    assert {"evolution.propose", "evolution.record_evaluation", "evolution.apply"} <= logged


def test_t1_api_evolution_rollback_routes_capability(
    api_client: TestClient, navi_home: Path
) -> None:
    """Execute a POST /v1/evolution_rollback request via api_client and verify it rolls back the evolution event."""
    # 1. Propose an evolution
    proposal_data = {
        "target_type": "memory_schema",
        "target_id": "rollback_policy",
        "reason": "E2E evolution rollback test",
        "expected_benefit": "rollback test benefit",
        "risk": "low",
        "before": "before rollback",
        "after": "after rollback",
        "rollback_plan": "rollback rollback",
        "eval_cases": ["rollback_case"],
    }
    response = api_client.post("/v1/evolution-proposals", json=proposal_data)
    assert response.status_code == 200
    proposal_id = response.json()["data"]["id"]

    # 2. Approve proposal
    eval_response = api_client.post(
        f"/v1/evolution-proposals/{proposal_id}/evaluation",
        json={"evaluation_result": "approved"},
    )
    assert eval_response.status_code == 200

    # 3. Apply proposal (generates an evolution event)
    apply_response = api_client.post(f"/v1/evolution-proposals/{proposal_id}/apply")
    assert apply_response.status_code == 200
    event_id = apply_response.json()["data"]["id"]

    # 4. Rollback the event
    rollback_response = api_client.post(f"/v1/evolution-events/{event_id}/rollback")
    assert rollback_response.status_code == 200

    rollback_data = rollback_response.json()["data"]
    assert rollback_data["id"] == event_id
    assert rollback_data["rolled_back_at"] > 0
    assert "evolution.rollback" in _logged_tools(navi_home)
