from __future__ import annotations

from pathlib import Path
from fastapi.testclient import TestClient

from navi.api import create_app
from navi.api_paths import api_path
from navi.config import load_config


def test_api_endpoints_core_routes(tmp_path: Path, valid_runtime_config):
    app = create_app(tmp_path)
    config = load_config(tmp_path)
    api_key = config.api.api_key
    headers = {"X-API-Key": api_key}
    client = TestClient(app)

    # 1. Health
    res = client.get(api_path("health"), headers=headers)
    assert res.status_code == 200
    assert res.json()["ok"] is True

    # 2. Tools
    res = client.get(api_path("tools"), headers=headers)
    assert res.status_code == 200
    assert "tools" in res.json()["data"]

    # 3. Approvals
    res = client.get(api_path("approvals"), headers=headers)
    assert res.status_code == 200
    assert "approvals" in res.json()["data"]

    # 4. Evolution targets
    res = client.get(api_path("evolution_targets"), headers=headers)
    assert res.status_code == 200
    assert "targets" in res.json()["data"]

    # 5. Evolution events
    res = client.get(api_path("evolution_events"), headers=headers)
    assert res.status_code == 200
    assert "events" in res.json()["data"]

    # 6. Evolution proposals
    res = client.get(api_path("evolution_proposals"), headers=headers)
    assert res.status_code == 200
    assert "proposals" in res.json()["data"]

    # 7. Connector status 404 for unknown
    unknown_path = api_path("connector_status").replace("{connector_name}", "unknown")
    res = client.get(unknown_path, headers=headers)
    assert res.status_code == 404

    # 8. Auth status
    res = client.get(api_path("auth_status"), headers=headers)
    assert res.status_code == 200
    assert "providers" in res.json()["data"]

    # 9. Diagnostics
    res = client.get(api_path("diagnostics"), headers=headers)
    assert res.status_code == 200
    assert "checks" in res.json()["data"]

    # 10. Metrics
    res = client.get(api_path("metrics"), headers=headers)
    assert res.status_code == 200

    # 11. Sessions & session aliases
    res = client.get(api_path("sessions"), headers=headers)
    assert res.status_code == 200
    res = client.get(api_path("session_aliases"), headers=headers)
    assert res.status_code == 200

    # 12. Memory & memory conflicts
    res = client.get(api_path("memory"), headers=headers)
    assert res.status_code == 200
    res = client.get(api_path("memory_conflicts"), headers=headers)
    assert res.status_code == 200

    # 13. Skills & graph
    res = client.get(api_path("skills"), headers=headers)
    assert res.status_code == 200
    res = client.get(api_path("graph"), headers=headers)
    assert res.status_code == 200

    # 14. Traces & trace evaluations
    res = client.get(api_path("traces"), headers=headers)
    assert res.status_code == 200
    res = client.get(api_path("trace_evaluations"), headers=headers)
    assert res.status_code == 200

    # 15. Goals
    res = client.get(api_path("goals"), headers=headers)
    assert res.status_code == 200
    assert "goals" in res.json()["data"]

    # 16. Single goal 404 for nonexistent
    goal_path = api_path("goal").replace("{goal_id}", "nonexistent")
    res = client.get(goal_path, headers=headers)
    assert res.status_code == 404

    # 17. Dynamic parameters: GET, POST set, rollback, reset
    res = client.get(api_path("dynamic_parameters"), headers=headers)
    assert res.status_code == 200
    assert "parameters" in res.json()["data"]

    res_set = client.post(
        api_path("dynamic_parameters_set"),
        json={"name": "provider_retry_after_seconds", "value": 45.0, "reason": "api_test"},
        headers=headers,
    )
    assert res_set.status_code == 200
    assert res_set.json()["data"]["value"] == 45.0

    res_rb = client.post(
        api_path("dynamic_parameters_rollback"),
        json={"name": "provider_retry_after_seconds", "reason": "api_rollback_test"},
        headers=headers,
    )
    assert res_rb.status_code == 200
    assert res_rb.json()["data"]["rolled_back"] is True

    res_reset = client.post(
        api_path("dynamic_parameters_reset"),
        json={"name": "provider_retry_after_seconds"},
        headers=headers,
    )
    assert res_reset.status_code == 200
    assert res_reset.json()["data"]["value"] == 15.0

    # 18. Replay buffer
    res_buf = client.get(api_path("replay_buffer"), headers=headers)
    assert res_buf.status_code == 200
    assert "entries" in res_buf.json()["data"]
    assert "total_count" in res_buf.json()["data"]

    # 19. Self play trials
    res_trials = client.get(api_path("self_play_trials"), headers=headers)
    assert res_trials.status_code == 200
    assert "trials" in res_trials.json()["data"]

    # 20. Self play cycle
    res_cycle = client.post(
        api_path("self_play_cycle"),
        json={"max_trials": 1, "auto_promote": False, "use_ema": False},
        headers=headers,
    )
    assert res_cycle.status_code == 200
    assert "trials" in res_cycle.json()["data"]
