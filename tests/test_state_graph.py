from __future__ import annotations

import json
import shlex
import sys
from dataclasses import replace

import pytest

from navi.capabilities import CapabilityRegistry
from navi.capabilities_types import CapabilityContext
from navi.loop_contracts import (
    GoalSpec,
    LoopSpec,
    LoopTerminalState,
    RetryPolicy,
    TimeoutPolicy,
    VerificationKind,
    VerificationStep,
)
from navi.provider import ChatMessage
from navi.runtime import AgentRuntime
from navi.state_graph import (
    CapabilityExecutorPort,
    DurableStateGraphRunner,
    ModelCapabilityPlannerPort,
)


def _command(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def _spec(command: str, *, timeout: float = 5.0) -> LoopSpec:
    return LoopSpec.from_goal(
        GoalSpec(
            objective="run durable state graph",
            scope=("repo:/tmp/project",),
            acceptance_criteria=("verification command passes",),
            permission_ceiling="read",
        ),
        goal_id="goal-1",
        allowed_capabilities=("test.run",),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.COMMAND_EXIT_CODE,
                name="verification",
                command=command,
                timeout=TimeoutPolicy(seconds=timeout),
            ),
        ),
    )


def _write_spec(command: str, *, timeout: float = 5.0) -> LoopSpec:
    return LoopSpec.from_goal(
        GoalSpec(
            objective="write app.py with the planned content",
            scope=("repo:/tmp/project",),
            acceptance_criteria=("planned file write is visible to verification",),
            permission_ceiling="write",
            owner="tester",
        ),
        goal_id="goal-1",
        allowed_capabilities=("file.write", "test.run"),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.COMMAND_EXIT_CODE,
                name="verification",
                command=command,
                timeout=TimeoutPolicy(seconds=timeout),
            ),
        ),
    )


class _PlanningProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        self.calls.append((role, messages))
        assert role == "planner"
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
                        "reason": "write requested file in shadow workspace",
                    }
                ]
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "executor", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class _RetryPlanningProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        self.calls += 1
        content = "wrong\n" if self.calls == 1 else "agent\n"
        return json.dumps(
            {
                "syscalls": [
                    {
                        "tool": "file.write",
                        "permission": "write",
                        "args": {
                            "path": "app.py",
                            "content": content,
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


def test_durable_state_graph_sync_runner_is_disabled(tmp_path):
    runner = DurableStateGraphRunner(home=tmp_path)

    with pytest.raises(RuntimeError, match="run\\(\\) is disabled"):
        runner.run(_spec(_command("print('ok')")), workspace=tmp_path)


@pytest.mark.asyncio
async def test_durable_state_graph_async_requires_explicit_ports(tmp_path):
    runner = DurableStateGraphRunner(home=tmp_path)

    with pytest.raises(RuntimeError, match="requires explicit planner_port and executor_port"):
        await runner.run_async(_spec(_command("print('ok')")), workspace=tmp_path)


@pytest.mark.asyncio
async def test_durable_state_graph_async_plan_execute_uses_llm_and_capability_port(tmp_path):
    provider = _PlanningProvider()
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    planner_capabilities = CapabilityRegistry(
        home=tmp_path,
        project_dir=tmp_path,
        permission_ceiling="write",
    )
    context = CapabilityContext(
        home=tmp_path,
        source="state_graph",
        peer_id="state_graph",
        sender_id="tester",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )
    runner = DurableStateGraphRunner(
        home=tmp_path,
        planner_port=ModelCapabilityPlannerPort(
            runtime=runtime,
            capabilities=planner_capabilities,
        ),
        executor_port=CapabilityExecutorPort(home=tmp_path, context=context),
    )

    result = await runner.run_async(
        _write_spec(_command("from pathlib import Path; assert Path('app.py').read_text() == 'agent\\n'")),
        workspace=tmp_path,
    )

    assert result.terminal_state == LoopTerminalState.CONVERGED
    assert provider.calls and provider.calls[0][0] == "planner"
    assert result.evidence["planned_capability"]["tool"] == "file.write"
    assert result.evidence["capability_result"]["ok"] is True
    assert result.evidence["capability_result"]["facts"]["state_transition"] == "written"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "agent\n"


@pytest.mark.asyncio
async def test_durable_state_graph_reflects_checker_failure_and_replans(tmp_path):
    provider = _RetryPlanningProvider()
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    planner_capabilities = CapabilityRegistry(
        home=tmp_path,
        project_dir=tmp_path,
        permission_ceiling="write",
    )
    context = CapabilityContext(
        home=tmp_path,
        source="state_graph",
        peer_id="state_graph",
        sender_id="tester",
        permission_ceiling="write",
        workspace=str(tmp_path),
    )
    runner = DurableStateGraphRunner(
        home=tmp_path,
        planner_port=ModelCapabilityPlannerPort(
            runtime=runtime,
            capabilities=planner_capabilities,
        ),
        executor_port=CapabilityExecutorPort(home=tmp_path, context=context),
    )
    spec = replace(
        _write_spec(_command("from pathlib import Path; assert Path('app.py').read_text() == 'agent\\n'")),
        retry_policy=RetryPolicy(max_attempts=2),
    )

    result = await runner.run_async(spec, workspace=tmp_path)

    assert result.terminal_state == LoopTerminalState.CONVERGED
    assert provider.calls == 2
    assert result.run_state.attempt == 2
    assert result.evidence["reflection"]["retry"] is True
    assert "recovery_fact" in result.evidence["reflection"]["facts"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "agent\n"
