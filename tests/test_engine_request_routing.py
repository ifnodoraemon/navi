from __future__ import annotations

import json
import shlex
import sys

import pytest

from navi.control_plane import TurnController
from navi.lifecycle import Resolution
from navi.loop_contracts import LoopTerminalState
from navi.loop_runs import LoopRunStore
from navi.provider import ChatMessage
from navi.runtime import AgentRuntime


class NoModelProvider:
    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        raise AssertionError(f"structured request routing should not call model role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class PlanningProvider:
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
                        "model_role": "executor",
                    }
                ]
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "executor", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class RouterThenPlanningProvider(PlanningProvider):
    enable_request_router = True

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        if role == "router":
            self.calls.append(role)
            return json.dumps(
                {
                    "intent": "open_goal",
                    "reason": "request requires durable code modification",
                    "confidence": 0.93,
                    "facts": {
                        "objective": "write app.py through model router",
                        "workspace": self.workspace,
                        "allowed_capabilities": ["file.write", "test.run"],
                        "verification_command": _command(
                            "from pathlib import Path; assert Path('app.py').read_text() == 'agent\\n'"
                        ),
                        "timeout_seconds": 5,
                        "auto_start": True,
                    },
                }
            )
        return await super().complete_for(role, messages, **kwargs)


def _command(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


@pytest.mark.asyncio
async def test_structured_open_goal_route_bypasses_planner(tmp_path):
    engine = TurnController(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=NoModelProvider()),
        project_dir=tmp_path,
        permission_ceiling="write",
    )

    result = await engine.handle(
        "implement a durable route",
        peer_id="local",
        sender_id="local",
        source="local",
        intent_facts={
            "request_routing_decision": {
                "intent": "open_goal",
                "reason": "structured protocol selected slow path",
                "confidence": 1.0,
                "facts": {
                    "objective": "implement a durable route",
                    "workspace": str(tmp_path),
                    "auto_start": False,
                },
            }
        },
    )

    assert result.ok is True
    assert result.model_role == "request_router"
    assert result.action == "goal"
    assert result.facts["state_transition"] == "opened"
    assert result.facts["loop_terminal_state"] == ""
    assert LoopRunStore(tmp_path).get_run(result.facts["loop_run_id"]) is not None


@pytest.mark.asyncio
async def test_structured_control_goal_cancel_route_bypasses_planner(tmp_path):
    engine = TurnController(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=NoModelProvider()),
        project_dir=tmp_path,
        permission_ceiling="write",
    )
    opened = await engine.handle(
        "create cancellable goal",
        peer_id="local",
        sender_id="local",
        source="local",
        intent_facts={
            "request_routing_decision": {
                "intent": "open_goal",
                "reason": "structured protocol selected slow path",
                "confidence": 1.0,
                "facts": {
                    "objective": "create cancellable goal",
                    "workspace": str(tmp_path),
                    "auto_start": False,
                },
            }
        },
    )

    cancelled = await engine.handle(
        "cancel it",
        peer_id="local",
        sender_id="local",
        source="local",
        intent_facts={
            "request_routing_decision": {
                "intent": "control_goal",
                "reason": "structured protocol selected cancel control",
                "confidence": 1.0,
                "goal_id": opened.facts["goal_id"],
                "facts": {"control": "cancel", "reason": "user stopped goal"},
            }
        },
    )

    assert cancelled.ok is True
    assert cancelled.model_role == "request_router"
    assert cancelled.facts["state_transition"] == "cancelled"
    assert cancelled.facts["loop_terminal_state"] == LoopTerminalState.CANCELLED
    assert cancelled.facts["resolution"] == Resolution.CANCELED


@pytest.mark.asyncio
async def test_structured_open_goal_auto_start_uses_durable_state_graph(tmp_path):
    provider = PlanningProvider()
    engine = TurnController(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        permission_ceiling="write",
    )

    result = await engine.handle(
        "write app.py through state graph",
        peer_id="local",
        sender_id="local",
        source="local",
        intent_facts={
            "request_routing_decision": {
                "intent": "open_goal",
                "reason": "structured protocol selected durable slow path",
                "confidence": 1.0,
                "facts": {
                    "objective": "write app.py through state graph",
                    "workspace": str(tmp_path),
                    "allowed_capabilities": ["file.write", "test.run"],
                    "verification_command": _command(
                        "from pathlib import Path; assert Path('app.py').read_text() == 'agent\\n'"
                    ),
                    "timeout_seconds": 5,
                    "auto_start": True,
                },
            }
        },
    )

    assert result.ok is True
    assert result.model_role == "request_router"
    assert provider.calls == ["planner"]
    assert result.facts["state_transition"] == "opened"
    assert result.facts["loop_terminal_state"] == LoopTerminalState.CONVERGED
    assert result.facts["completion_evidence"] is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "agent\n"


@pytest.mark.asyncio
async def test_model_request_router_can_open_goal_into_state_graph(tmp_path):
    provider = RouterThenPlanningProvider()
    provider.workspace = str(tmp_path)
    engine = TurnController(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
        permission_ceiling="write",
    )

    result = await engine.handle(
        "write app.py using the new durable route",
        peer_id="local",
        sender_id="local",
        source="local",
    )

    assert result.ok is True
    assert provider.calls == ["router", "planner"]
    assert result.model_role == "request_router"
    assert result.facts["loop_terminal_state"] == LoopTerminalState.CONVERGED
    assert result.facts["completion_evidence"] is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "agent\n"
