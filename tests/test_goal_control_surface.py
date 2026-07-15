from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

import navi.api as api_module
import navi.cli as cli_module
from navi.api_paths import api_path
from navi.lifecycle import Resolution
from navi.loop_contracts import LoopTerminalState
from navi.provider import ChatMessage
from navi.runtime import AgentRuntime
from navi.runs import RunStore


def _command(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def _approve_pending(home: Path, run_id: str) -> None:
    runs = RunStore(home)
    approval = runs.pending_approval_for_run(run_id)
    assert approval is not None
    resolved = runs.resolve_approval(
        approval.id,
        decision="approve",
        resolved_by="tester",
    )
    assert resolved is not None


class _PlanningProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        self.calls.append(role)
        return json.dumps(
            {
                "syscalls": [
                    {
                        "tool": "file.write",
                        "permission": "write",
                        "args": {
                            "path": "app.py",
                            "content": "agent\n",
                            "mode": "overwrite",
                            "create_dirs": True,
                        },
                        "reason": "write the requested file before verification",
                    }
                ]
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "executor", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


def test_goal_api_exposes_open_state_and_cancel_controls(tmp_path):
    app = api_module.create_app(tmp_path)
    api_key = (tmp_path / "api_key").read_text(encoding="utf-8").strip()
    headers = {"X-API-Key": api_key}
    client = TestClient(app)

    opened_response = client.post(
        api_path("goals"),
        json={
            "objective": "api durable goal",
            "workspace": str(tmp_path),
            "auto_start": False,
        },
        headers=headers,
    )
    assert opened_response.status_code == 200, opened_response.text
    opened = opened_response.json()["data"]["facts"]
    assert opened["state_transition"] == "opened"
    assert opened["loop_terminal_state"] == ""

    state_response = client.get(
        api_path("goal_state").format(goal_id=opened["goal_id"]),
        headers=headers,
    )
    assert state_response.status_code == 200, state_response.text
    state = state_response.json()["data"]["facts"]
    assert state["state_transition"] == "state_read"
    assert state["goal"]["id"] == opened["goal_id"]
    assert state["loop_runs"][0]["run_id"] == opened["loop_run_id"]

    cancel_response = client.post(
        api_path("goal_cancel").format(goal_id=opened["goal_id"]),
        json={"reason": "api cancel"},
        headers=headers,
    )
    assert cancel_response.status_code == 200, cancel_response.text
    cancelled = cancel_response.json()["data"]["facts"]
    assert cancelled["state_transition"] == "cancelled"
    assert cancelled["loop_terminal_state"] == LoopTerminalState.CANCELLED
    assert cancelled["resolution"] == Resolution.CANCELED


def test_goal_cli_exposes_open_state_and_cancel_controls(tmp_path):
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    opened_result = runner.invoke(
        cli_module.app,
        [
            "goal",
            "open",
            "cli durable goal",
            "--workspace",
            str(tmp_path),
            "--no-auto-start",
        ],
        env=env,
    )
    assert opened_result.exit_code == 0, opened_result.output
    opened = json.loads(opened_result.output)
    assert opened["state_transition"] == "opened"
    assert opened["loop_terminal_state"] == ""

    state_result = runner.invoke(cli_module.app, ["goal", "state", opened["goal_id"]], env=env)
    assert state_result.exit_code == 0, state_result.output
    state = json.loads(state_result.output)
    assert state["goal"]["id"] == opened["goal_id"]
    assert state["loop_runs"][0]["run_id"] == opened["loop_run_id"]

    cancel_result = runner.invoke(
        cli_module.app,
        ["goal", "cancel", opened["goal_id"], "--reason", "cli cancel"],
        env=env,
    )
    assert cancel_result.exit_code == 0, cancel_result.output
    cancelled = json.loads(cancel_result.output)
    assert cancelled["state_transition"] == "cancelled"
    assert cancelled["loop_terminal_state"] == LoopTerminalState.CANCELLED
    assert cancelled["resolution"] == Resolution.CANCELED


def test_goal_api_auto_start_uses_runtime_state_graph(tmp_path, monkeypatch):
    provider = _PlanningProvider()
    monkeypatch.setattr(
        api_module,
        "build_runtime",
        lambda home: AgentRuntime(home=home, provider=provider),
    )
    app = api_module.create_app(tmp_path)
    api_key = (tmp_path / "api_key").read_text(encoding="utf-8").strip()
    headers = {"X-API-Key": api_key}
    client = TestClient(app)

    opened_response = client.post(
        api_path("goals"),
        json={
            "objective": "api auto-start goal writes app.py",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["file.write", "shell.run"],
            "verification_command": _command(
                "from pathlib import Path; assert Path('app.py').read_text() == 'agent\\n'"
            ),
            "timeout_seconds": 5,
        },
        headers=headers,
    )

    assert opened_response.status_code == 200, opened_response.text
    opened = opened_response.json()["data"]["facts"]
    assert provider.calls == ["planner"]
    assert opened["state_transition"] == "opened"
    assert opened["loop_terminal_state"] == LoopTerminalState.WAITING_APPROVAL
    assert opened["completion_evidence"] is False
    evidence = opened["state_graph_result"]["evidence"]
    assert evidence["planned_capability"]["tool"] == "file.write"
    assert evidence["capability_result"]["yields_control"] is True
    _approve_pending(tmp_path, opened["run_id"])
    resumed_response = client.post(
        api_path("goal_resume").format(goal_id=opened["goal_id"]),
        json={"workspace": str(tmp_path)},
        headers=headers,
    )
    assert resumed_response.status_code == 200, resumed_response.text
    completed = resumed_response.json()["data"]["facts"]
    assert provider.calls == ["planner"]
    assert completed["loop_terminal_state"] == LoopTerminalState.CONVERGED
    assert completed["completion_evidence"] is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "agent\n"


def test_goal_api_resume_uses_runtime_state_graph(tmp_path, monkeypatch):
    provider = _PlanningProvider()
    monkeypatch.setattr(
        api_module,
        "build_runtime",
        lambda home: AgentRuntime(home=home, provider=provider),
    )
    app = api_module.create_app(tmp_path)
    api_key = (tmp_path / "api_key").read_text(encoding="utf-8").strip()
    headers = {"X-API-Key": api_key}
    client = TestClient(app)

    opened_response = client.post(
        api_path("goals"),
        json={
            "objective": "api resume goal writes app.py",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["file.write", "shell.run"],
            "verification_command": _command(
                "from pathlib import Path; assert Path('app.py').read_text() == 'agent\\n'"
            ),
            "auto_start": False,
            "timeout_seconds": 5,
        },
        headers=headers,
    )
    assert opened_response.status_code == 200, opened_response.text
    opened = opened_response.json()["data"]["facts"]
    assert opened["loop_terminal_state"] == ""

    resumed_response = client.post(
        api_path("goal_resume").format(goal_id=opened["goal_id"]),
        json={"workspace": str(tmp_path)},
        headers=headers,
    )

    assert resumed_response.status_code == 200, resumed_response.text
    resumed = resumed_response.json()["data"]["facts"]
    assert provider.calls == ["planner"]
    assert resumed["state_transition"] == "resumed"
    assert resumed["loop_terminal_state"] == LoopTerminalState.WAITING_APPROVAL
    evidence = resumed["state_graph_result"]["evidence"]
    assert evidence["planned_capability"]["tool"] == "file.write"
    assert evidence["capability_result"]["yields_control"] is True
    _approve_pending(tmp_path, resumed["run_id"])
    completed_response = client.post(
        api_path("goal_resume").format(goal_id=opened["goal_id"]),
        json={"workspace": str(tmp_path)},
        headers=headers,
    )
    assert completed_response.status_code == 200, completed_response.text
    completed = completed_response.json()["data"]["facts"]
    assert provider.calls == ["planner"]
    assert completed["loop_terminal_state"] == LoopTerminalState.CONVERGED
    assert completed["completion_evidence"] is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "agent\n"


def test_goal_cli_auto_start_uses_runtime_state_graph(tmp_path, monkeypatch):
    provider = _PlanningProvider()
    monkeypatch.setattr(
        cli_module,
        "build_runtime",
        lambda home: AgentRuntime(home=home, provider=provider),
    )
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    opened_result = runner.invoke(
        cli_module.app,
        [
            "goal",
            "open",
            "cli auto-start goal writes app.py",
            "--workspace",
            str(tmp_path),
            "--allowed-capability",
            "file.write",
            "--allowed-capability",
            "shell.run",
            "--verification-command",
            _command("from pathlib import Path; assert Path('app.py').read_text() == 'agent\\n'"),
            "--timeout-seconds",
            "5",
        ],
        env=env,
    )

    assert opened_result.exit_code == 0, opened_result.output
    opened = json.loads(opened_result.output)
    assert provider.calls == ["planner"]
    assert opened["state_transition"] == "opened"
    assert opened["loop_terminal_state"] == LoopTerminalState.WAITING_APPROVAL
    assert opened["completion_evidence"] is False
    evidence = opened["state_graph_result"]["evidence"]
    assert evidence["planned_capability"]["tool"] == "file.write"
    assert evidence["capability_result"]["yields_control"] is True
    _approve_pending(tmp_path, opened["run_id"])
    resumed_result = runner.invoke(
        cli_module.app,
        ["goal", "resume", opened["goal_id"], "--workspace", str(tmp_path)],
        env=env,
    )
    assert resumed_result.exit_code == 0, resumed_result.output
    completed = json.loads(resumed_result.output)
    assert provider.calls == ["planner"]
    assert completed["loop_terminal_state"] == LoopTerminalState.CONVERGED
    assert completed["completion_evidence"] is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "agent\n"


def test_goal_cli_resume_uses_runtime_state_graph(tmp_path, monkeypatch):
    provider = _PlanningProvider()
    monkeypatch.setattr(
        cli_module,
        "build_runtime",
        lambda home: AgentRuntime(home=home, provider=provider),
    )
    runner = CliRunner()
    env = {"NAVI_HOME": str(tmp_path)}

    opened_result = runner.invoke(
        cli_module.app,
        [
            "goal",
            "open",
            "cli resume goal writes app.py",
            "--workspace",
            str(tmp_path),
            "--allowed-capability",
            "file.write",
            "--allowed-capability",
            "shell.run",
            "--verification-command",
            _command("from pathlib import Path; assert Path('app.py').read_text() == 'agent\\n'"),
            "--timeout-seconds",
            "5",
            "--no-auto-start",
        ],
        env=env,
    )
    assert opened_result.exit_code == 0, opened_result.output
    opened = json.loads(opened_result.output)
    assert opened["loop_terminal_state"] == ""

    resumed_result = runner.invoke(
        cli_module.app,
        ["goal", "resume", opened["goal_id"], "--workspace", str(tmp_path)],
        env=env,
    )

    assert resumed_result.exit_code == 0, resumed_result.output
    resumed = json.loads(resumed_result.output)
    assert provider.calls == ["planner"]
    assert resumed["state_transition"] == "resumed"
    assert resumed["loop_terminal_state"] == LoopTerminalState.WAITING_APPROVAL
    evidence = resumed["state_graph_result"]["evidence"]
    assert evidence["planned_capability"]["tool"] == "file.write"
    assert evidence["capability_result"]["yields_control"] is True
    _approve_pending(tmp_path, resumed["run_id"])
    completed_result = runner.invoke(
        cli_module.app,
        ["goal", "resume", opened["goal_id"], "--workspace", str(tmp_path)],
        env=env,
    )
    assert completed_result.exit_code == 0, completed_result.output
    completed = json.loads(completed_result.output)
    assert provider.calls == ["planner"]
    assert completed["loop_terminal_state"] == LoopTerminalState.CONVERGED
    assert completed["completion_evidence"] is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "agent\n"
