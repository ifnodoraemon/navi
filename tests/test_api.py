from __future__ import annotations

import os
import re

os.environ["NAVI_API_KEY"] = "test_key"

from fastapi.testclient import TestClient

from navi.provider import ChatMessage, MockProvider, ModelPool
from navi.runtime import AgentRuntime
from navi.api import create_app


def authenticated_client(app, headers: dict[str, str] | None = None) -> TestClient:
    merged = {"X-API-Key": "test_key"}
    if headers:
        merged.update(headers)
    return TestClient(app, headers=merged)


class ScriptedProvider(MockProvider):
    def __init__(self, response: str | list[str]):
        self.response = response
        self.messages: list[ChatMessage] = []

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.messages = messages
        if isinstance(self.response, list):
            response = self.response.pop(0)
        else:
            response = self.response
        if "TASK_ID" in response:
            for message in reversed(messages):
                match = re.search(r'"run_id":\s*"([^"]+)"', message.content)
                if match:
                    return response.replace("TASK_ID", match.group(1))
        return response


def test_headless_local_api_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    client = authenticated_client(create_app(tmp_path))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["model_provider"] == "mock"

    chat = client.post("/v1/chat", json={"message": "hello"})
    assert chat.status_code == 200
    chat_data = chat.json()
    assert chat_data["session_id"]
    assert chat_data["message"] == "Navi received: hello"

    sessions = client.get("/v1/sessions")
    assert sessions.status_code == 200
    assert chat_data["session_id"] in sessions.json()["sessions"]

    new_session = client.post("/v1/sessions", json={"alias": "api:test"})
    assert new_session.status_code == 200
    assert new_session.json()["session_id"]
    aliases = client.get("/v1/session-aliases")
    assert aliases.status_code == 200
    assert aliases.json()["aliases"][0]["alias"] == "api:test"

    session = client.get(f"/v1/sessions/{chat_data['session_id']}")
    assert session.status_code == 200
    assert [message["role"] for message in session.json()["messages"]] == ["user", "assistant"]

    memory = client.post("/v1/memory", json={"text": "Prefers direct answers"})
    assert memory.status_code == 200
    assert "Prefers direct answers" in client.get("/v1/memory").json()["memory"]

    created = client.post("/v1/delegations", json={"title": "Test the console"})
    assert created.status_code == 200
    task = created.json()
    assert task["status"] == "awaiting_approval"
    goals = client.get("/v1/goals", params={"status": "awaiting_approval"})
    assert goals.status_code == 200
    goal = goals.json()["goals"][0]
    assert goal["run_id"] == task["id"]
    shown_goal = client.get(f"/v1/goals/{goal['id']}")
    assert shown_goal.status_code == 200
    assert shown_goal.json()["events"][0]["event_type"] == "goal.created"

    updated = client.patch(f"/v1/delegations/{task['id']}", json={"status": "active"})
    assert updated.status_code == 409
    queued = client.patch(f"/v1/delegations/{task['id']}", json={"status": "queued"})
    assert queued.status_code == 200
    assert queued.json()["status"] == "queued"
    assert client.get("/v1/delegations").json()["delegations"][0]["title"] == "Test the console"

    deleted = client.delete(f"/v1/delegations/{task['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get("/v1/delegations").json()["delegations"] == []

    wx_status = client.get("/v1/connectors/weixin/status")
    assert wx_status.status_code == 200
    assert wx_status.json()["configured"] is False

    tools = client.get("/v1/tools")
    assert tools.status_code == 200
    assert "delegate.status" in {tool["name"] for tool in tools.json()["tools"]}
    assert "delegate.spawn" in {node["name"] for node in tools.json()["capabilities"]}
    assert "core" in tools.json()["sources"]

    provider = client.post("/v1/tools/provider.config/call", json={"args": {}})
    assert provider.status_code == 200
    assert provider.json()["ok"] is True
    assert provider.json()["facts"]["provider"] == "mock"

    write_tool = client.post(
        "/v1/tools/file.write/call",
        json={"args": {"path": "api-write.txt", "content": "blocked"}},
    )
    assert write_tool.status_code == 409
    assert "read-only" in write_tool.json()["detail"]

    diagnostics = client.get("/v1/diagnostics")
    assert diagnostics.status_code == 200
    checks = {check["name"]: check for check in diagnostics.json()["checks"]}
    assert checks["config.validation"]["status"] == "ok"
    assert "capabilities" in checks
    diagnostics_with_connectivity = client.get("/v1/diagnostics", params={"connectivity": "true"})
    connectivity_checks = {
        check["name"]: check for check in diagnostics_with_connectivity.json()["checks"]
    }
    assert connectivity_checks["api.model.connectivity"]["status"] == "ok"

    missing_tool = client.post("/v1/tools/missing.tool/call", json={"args": {}})
    assert missing_tool.status_code == 404


def test_chat_api_routes_natural_language_task_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    provider = ScriptedProvider(
        [
            '{"tool":"delegate.spawn","permission":"prepare","args":{"prompt":"检查本地服务状态"},"confidence":0.95,"reason":"local action request"}',
            '{"tool":"delegate.prepare","permission":"prepare","args":{"run_id":"TASK_ID"},"confidence":0.95,"reason":"prepare task"}',
            '{"tool":"approval.request","permission":"prepare","args":{"run_id":"TASK_ID"},"confidence":0.95,"reason":"request approval"}',
            '{"tool":"final.answer","permission":"read","args":{"message":"已为你创建受控任务，等待审批后执行。"},"confidence":0.95,"reason":"task prepared"}',
        ]
    )

    import navi.api as api_module

    monkeypatch.setattr(
        api_module,
        "build_runtime",
        lambda home=None: AgentRuntime(home=tmp_path, provider=ModelPool(default=provider)),
    )
    client = authenticated_client(api_module.create_app(tmp_path))

    response = client.post("/v1/chat", json={"message": "帮我检查本地服务状态"})

    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "approval"
    assert data["run_id"]
    task = client.get("/v1/delegations").json()["delegations"][0]
    assert task["prompt"] == "检查本地服务状态"
    assert task["status"] == "awaiting_approval"
    from navi.runs import RunStore

    code = RunStore(tmp_path).list_approvals()[0].code
    assert f"审批码: `{code}`" in data["message"]
    assert f"批准 {code}" in data["message"]


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
    client = authenticated_client(api_module.create_app(tmp_path))

    response = client.post("/v1/chat", json={"message": "navi 服务状态如何"})

    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "tool"
    assert "ActiveState: active" in data["message"]


def test_chat_api_routes_natural_language_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    provider = ScriptedProvider(
        [
            '{"tool":"delegate.spawn","permission":"prepare","args":{"prompt":"检查本地服务状态"},"confidence":0.95,"reason":"local action request"}',
            '{"tool":"delegate.prepare","permission":"prepare","args":{"run_id":"TASK_ID"},"confidence":0.95,"reason":"prepare task"}',
            '{"tool":"approval.request","permission":"prepare","args":{"run_id":"TASK_ID"},"confidence":0.95,"reason":"request approval"}',
            '{"tool":"final.answer","permission":"read","args":{"message":"已创建任务，等待审批。"},"confidence":0.95,"reason":"task prepared"}',
        ]
    )

    import navi.api as api_module

    monkeypatch.setattr(
        api_module,
        "build_runtime",
        lambda home=None: AgentRuntime(home=tmp_path, provider=ModelPool(default=provider)),
    )
    client = authenticated_client(api_module.create_app(tmp_path))
    created = client.post("/v1/chat", json={"message": "帮我检查本地服务状态"})
    run_id = created.json()["run_id"]

    from navi.runs import RunStore

    code = RunStore(tmp_path).list_approvals()[0].code
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
    assert data["run_id"] == run_id
    assert RunStore(tmp_path).get(run_id).status == "queued"


def test_active_api_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    client = authenticated_client(create_app(tmp_path))

    created = client.post(
        "/v1/active/delegations",
        json={"prompt": "active api task", "peer_id": "local", "sender_id": "local"},
    )

    assert created.status_code == 200
    created_data = created.json()
    assert "prepared for approval" in created_data["message"]
    task = created_data["delegation"]
    assert task["status"] == "awaiting_approval"

    approved = client.post(f"/v1/delegations/{task['id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "queued"

    processed = client.post("/v1/delegations/process")
    assert processed.status_code == 200
    assert processed.json()["delegations"][0]["status"] == "completed"
    subagents = client.get("/v1/subagents", params={"run_id": task["id"]})
    assert subagents.status_code == 200
    roles = {item["role"] for item in subagents.json()["subagents"]}
    assert {"executor", "critic"} <= roles
    shown = client.get(f"/v1/subagents/{subagents.json()['subagents'][0]['id']}")
    assert shown.status_code == 200
    assert shown.json()["subagent"]["status"] in {"completed", "failed"}

    assert client.get("/v1/graph").json()["nodes"]
    assert client.get("/v1/evolution-events").json()["events"]


def test_task_process_blocks_queued_task_without_execution_grant(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")
    client = authenticated_client(create_app(tmp_path))

    created = client.post("/v1/delegations", json={"title": "manual task"})
    task = created.json()
    from navi.runs import RunStore

    RunStore(tmp_path).update_run(task["id"], status="queued")

    processed = client.post("/v1/delegations/process")

    assert processed.status_code == 200
    assert processed.json()["delegations"][0]["status"] == "blocked"


def test_active_watch_api(tmp_path):
    client = authenticated_client(create_app(tmp_path))

    created = client.post(
        "/v1/active/watches",
        json={"cron": "*/10 * * * *", "prompt": "check active watches"},
    )

    assert created.status_code == 200
    assert "Watch" in created.json()["message"]
    assert client.get("/v1/watches").json()["watches"][0]["prompt"] == "check active watches"


def test_trace_api_flow(tmp_path):
    client = authenticated_client(create_app(tmp_path))

    from navi.trace import TraceStore

    trace_id = "api-trace"
    TraceStore(tmp_path).add_event(
        trace_id=trace_id,
        phase="capability.result",
        tool="delegate.run",
        ok=False,
        message="missing grant",
    )

    traces = client.get("/v1/traces")
    assert traces.status_code == 200
    assert trace_id in traces.json()["trace_ids"]

    events = client.get(f"/v1/traces/{trace_id}")
    assert events.status_code == 200
    assert events.json()["events"][0]["tool"] == "delegate.run"

    evaluation = client.post(f"/v1/traces/{trace_id}/evaluate")
    assert evaluation.status_code == 200
    assert evaluation.json()["failure_domain"] == "tool_or_capability"


def test_evolution_proposal_api_flow(tmp_path):
    client = authenticated_client(create_app(tmp_path))

    targets = client.get("/v1/evolution-targets")
    assert targets.status_code == 200
    assert "memory_schema" in {target["target_type"] for target in targets.json()["targets"]}

    invalid = client.post(
        "/v1/evolution-proposals",
        json={
            "target_type": "unknown",
            "target_id": "target",
            "reason": "coverage",
            "after": "after",
        },
    )
    assert invalid.status_code == 409

    created = client.post(
        "/v1/evolution-proposals",
        json={
            "target_type": "memory_schema",
            "target_id": "policy",
            "reason": "cover proposal review",
            "expected_benefit": "more measurable changes",
            "risk": "low",
            "before": "old",
            "after": "new",
            "rollback_plan": "restore old",
            "eval_cases": ["record_task_without_preparation"],
        },
    )
    assert created.status_code == 200
    proposal_id = created.json()["id"]

    listed = client.get("/v1/evolution-proposals", params={"status": "proposed"})
    assert listed.status_code == 200
    assert listed.json()["proposals"][0]["id"] == proposal_id

    recorded = client.post(
        f"/v1/evolution-proposals/{proposal_id}/evaluation",
        json={"evaluation_result": "passed targeted evals"},
    )
    assert recorded.status_code == 200
    assert recorded.json()["evaluation_result"] == "passed targeted evals"

    applied = client.post(f"/v1/evolution-proposals/{proposal_id}/apply")
    assert applied.status_code == 200
    assert applied.json()["target_type"] == "memory_schema"

    rollback = client.post(f"/v1/evolution-events/{applied.json()['id']}/rollback")
    assert rollback.status_code == 200
    assert rollback.json()["rolled_back_at"] > 0

    assert client.post("/v1/evolution-proposals/missing/apply").status_code == 404
    assert client.post(
        "/v1/evolution-proposals/missing/evaluation",
        json={"evaluation_result": "missing"},
    ).status_code == 404


def test_api_auth_missing_and_invalid(tmp_path):
    # Test client with invalid key
    client_invalid = TestClient(create_app(tmp_path), headers={"X-API-Key": "invalid_key"})
    response = client_invalid.get("/v1/delegations")
    assert response.status_code == 401

    # Test client with missing key
    client_missing = TestClient(create_app(tmp_path))
    response = client_missing.get("/v1/delegations")
    assert response.status_code == 401

    response = client_missing.get("/")
    assert response.status_code == 401
