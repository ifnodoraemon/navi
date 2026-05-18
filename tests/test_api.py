from __future__ import annotations

from fastapi.testclient import TestClient

from navi.api import create_app


def test_local_console_api_flow(tmp_path):
    client = TestClient(create_app(tmp_path))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["model_provider"] == "mock"

    index = client.get("/")
    assert index.status_code == 200
    assert "id=\"sessions\"" in index.text
    assert "id=\"memory-form\"" in index.text
    assert "id=\"task-form\"" in index.text

    chat = client.post("/v1/chat", json={"message": "hello"})
    assert chat.status_code == 200
    chat_data = chat.json()
    assert chat_data["session_id"]
    assert chat_data["message"] == "Navi received: hello"

    sessions = client.get("/v1/sessions")
    assert sessions.status_code == 200
    assert chat_data["session_id"] in sessions.json()["sessions"]

    session = client.get(f"/v1/sessions/{chat_data['session_id']}")
    assert session.status_code == 200
    assert [message["role"] for message in session.json()["messages"]] == ["user", "assistant"]

    memory = client.post("/v1/memory", json={"text": "Prefers direct answers"})
    assert memory.status_code == 200
    assert "Prefers direct answers" in client.get("/v1/memory").json()["memory"]

    created = client.post("/v1/tasks", json={"title": "Test the console"})
    assert created.status_code == 200
    task = created.json()
    assert task["status"] == "pending"

    updated = client.patch(f"/v1/tasks/{task['id']}", json={"status": "active"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "active"
    assert client.get("/v1/tasks").json()["tasks"][0]["title"] == "Test the console"

    wx_status = client.get("/v1/weixin/status")
    assert wx_status.status_code == 200
    assert wx_status.json()["configured"] is False


def test_active_api_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_CODEX_MOCK", "true")
    client = TestClient(create_app(tmp_path))

    created = client.post(
        "/v1/active/tasks",
        json={"prompt": "active api task", "peer_id": "web", "sender_id": "web"},
    )

    assert created.status_code == 200
    created_data = created.json()
    assert "Why now:" in created_data["message"]
    task = created_data["task"]
    assert task["status"] == "awaiting_approval"

    approved = client.post(f"/v1/tasks/{task['id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "queued"

    processed = client.post("/v1/tasks/process")
    assert processed.status_code == 200
    assert processed.json()["tasks"][0]["status"] == "completed"

    assert client.get("/v1/graph").json()["nodes"]
    assert client.get("/v1/trust-rules").json()["trust_rules"]
    assert client.get("/v1/evolution-events").json()["events"]


def test_active_watch_api(tmp_path):
    client = TestClient(create_app(tmp_path))

    created = client.post(
        "/v1/active/watches",
        json={"cron": "*/10 * * * *", "prompt": "check active watches"},
    )

    assert created.status_code == 200
    assert "Watch" in created.json()["message"]
    assert client.get("/v1/watches").json()["watches"][0]["prompt"] == "check active watches"
