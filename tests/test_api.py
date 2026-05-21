from __future__ import annotations

from fastapi.testclient import TestClient

from navi.provider import ChatMessage, MockProvider, ModelPool
from navi.runtime import AgentRuntime
from navi.api import create_app


class ScriptedProvider(MockProvider):
    def __init__(self, response: str | list[str]):
        self.response = response
        self.messages: list[ChatMessage] = []

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.messages = messages
        if isinstance(self.response, list):
            return self.response.pop(0)
        return self.response


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

    new_session = client.post("/v1/sessions", json={"alias": "web:test"})
    assert new_session.status_code == 200
    assert new_session.json()["session_id"]
    aliases = client.get("/v1/session-aliases")
    assert aliases.status_code == 200
    assert aliases.json()["aliases"][0]["alias"] == "web:test"

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
    queued = client.patch(f"/v1/tasks/{task['id']}", json={"status": "queued"})
    assert queued.status_code == 409
    assert client.get("/v1/tasks").json()["tasks"][0]["title"] == "Test the console"

    deleted = client.delete(f"/v1/tasks/{task['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get("/v1/tasks").json()["tasks"] == []

    wx_status = client.get("/v1/connectors/weixin/status")
    assert wx_status.status_code == 200
    assert wx_status.json()["configured"] is False

    tools = client.get("/v1/tools")
    assert tools.status_code == 200
    assert "task.status" in {tool["name"] for tool in tools.json()["tools"]}
    assert "core" in tools.json()["sources"]

    provider = client.post("/v1/tools/provider.config/call", json={"args": {}})
    assert provider.status_code == 200
    assert provider.json()["ok"] is True
    assert provider.json()["facts"]["provider"] == "mock"

    missing_tool = client.post("/v1/tools/missing.tool/call", json={"args": {}})
    assert missing_tool.status_code == 404


def test_chat_api_routes_natural_language_task_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    provider = ScriptedProvider(
        [
            '{"tool":"task.create","permission":"prepare","args":{"prompt":"检查本地服务状态"},"confidence":0.95,"reason":"local action request"}',
            '{"tool":"final.answer","permission":"read","args":{"message":"已为你创建受控任务，等待审批后执行。"},"confidence":0.95,"reason":"task prepared"}',
        ]
    )

    import navi.api as api_module

    monkeypatch.setattr(
        api_module,
        "build_runtime",
        lambda home=None: AgentRuntime(home=tmp_path, provider=ModelPool(default=provider)),
    )
    client = TestClient(api_module.create_app(tmp_path))

    response = client.post("/v1/chat", json={"message": "帮我检查本地服务状态"})

    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "task"
    assert data["task_id"]
    task = client.get("/v1/tasks").json()["tasks"][0]
    assert task["prompt"] == "检查本地服务状态"
    assert task["status"] == "awaiting_approval"


def test_chat_api_routes_natural_language_service_status(tmp_path, monkeypatch):
    provider = ScriptedProvider(
        [
            '{"tool":"service.status","permission":"read","args":{"name":"navi.service"},"confidence":0.95,"reason":"status lookup"}',
            '{"tool":"final.answer","permission":"read","args":{"message":"navi.service 当前 ActiveState: active。"},"confidence":0.95,"reason":"facts observed"}',
        ]
    )

    import navi.api as api_module
    import navi.core_tools as tools_module
    from navi.fact_tools import ServiceFacts

    monkeypatch.setattr(
        api_module,
        "build_runtime",
        lambda home=None: AgentRuntime(home=tmp_path, provider=ModelPool(default=provider)),
    )
    monkeypatch.setattr(
        tools_module,
        "service_facts",
        lambda name: ServiceFacts(
            name=name,
            properties={"ActiveState": "active", "SubState": "running", "MainPID": "123"},
            exit_code=0,
            stderr="",
        ),
    )
    client = TestClient(api_module.create_app(tmp_path))

    response = client.post("/v1/chat", json={"message": "navi 服务状态如何"})

    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "tool"
    assert "ActiveState: active" in data["message"]


def test_chat_api_routes_natural_language_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    provider = ScriptedProvider(
        [
            '{"tool":"task.create","permission":"prepare","args":{"prompt":"检查本地服务状态"},"confidence":0.95,"reason":"local action request"}',
            '{"tool":"final.answer","permission":"read","args":{"message":"已创建任务，等待审批。"},"confidence":0.95,"reason":"task prepared"}',
        ]
    )

    import navi.api as api_module

    monkeypatch.setattr(
        api_module,
        "build_runtime",
        lambda home=None: AgentRuntime(home=tmp_path, provider=ModelPool(default=provider)),
    )
    client = TestClient(api_module.create_app(tmp_path))
    created = client.post("/v1/chat", json={"message": "帮我检查本地服务状态"})
    task_id = created.json()["task_id"]

    from navi.tasks import TaskStore

    code = TaskStore(tmp_path).list_approvals()[0].code
    provider.response = [
        (
            '{"tool":"approval.resolve","permission":"write","args":{"decision":"approve","code":"'
            + code
            + '"},"confidence":0.95,"reason":"explicit approval"}'
        ),
        '{"tool":"final.answer","permission":"read","args":{"message":"已批准任务并加入执行队列。"},"confidence":0.95,"reason":"approval resolved"}',
    ]

    approved = client.post("/v1/chat", json={"message": f"批准 {code}"})

    assert approved.status_code == 200
    data = approved.json()
    assert data["action"] == "approval"
    assert data["task_id"] == task_id
    assert TaskStore(tmp_path).get(task_id).status == "queued"


def test_active_api_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    client = TestClient(create_app(tmp_path))

    created = client.post(
        "/v1/active/tasks",
        json={"prompt": "active api task", "peer_id": "web", "sender_id": "web"},
    )

    assert created.status_code == 200
    created_data = created.json()
    assert "prepared for approval" in created_data["message"]
    task = created_data["task"]
    assert task["status"] == "awaiting_approval"

    approved = client.post(f"/v1/tasks/{task['id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "queued"

    processed = client.post("/v1/tasks/process")
    assert processed.status_code == 200
    assert processed.json()["tasks"][0]["status"] == "completed"

    assert client.get("/v1/graph").json()["nodes"]
    assert client.get("/v1/evolution-events").json()["events"]


def test_task_process_blocks_queued_task_without_execution_grant(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    client = TestClient(create_app(tmp_path))

    created = client.post("/v1/tasks", json={"title": "manual task"})
    task = created.json()
    from navi.tasks import TaskStore

    TaskStore(tmp_path).update_task(task["id"], status="queued")

    processed = client.post("/v1/tasks/process")

    assert processed.status_code == 200
    assert processed.json()["tasks"][0]["status"] == "blocked"


def test_active_watch_api(tmp_path):
    client = TestClient(create_app(tmp_path))

    created = client.post(
        "/v1/active/watches",
        json={"cron": "*/10 * * * *", "prompt": "check active watches"},
    )

    assert created.status_code == 200
    assert "Watch" in created.json()["message"]
    assert client.get("/v1/watches").json()["watches"][0]["prompt"] == "check active watches"
