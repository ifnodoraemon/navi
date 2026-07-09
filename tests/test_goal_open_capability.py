from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.goals import GoalStore
from navi.lifecycle import Phase, Resolution
from navi.loop_contracts import LoopTerminalState
from navi.loop_runs import LoopRunStore
from navi.provider import ChatMessage
from navi.runtime import AgentRuntime
from navi.runs import RunStore
from navi.trace import TraceStore


def _command(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def _context(home: Path, *, trace_id: str = "") -> CapabilityContext:
    return CapabilityContext(
        home=home,
        source="cli",
        peer_id="cli",
        sender_id="tester",
        permission_ceiling="write",
        workspace=str(home),
        trace_id=trace_id,
    )


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
                        "model_role": "executor",
                        "reason": "write the requested file before verification",
                    }
                ]
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "executor", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


@pytest.mark.asyncio
async def test_goal_open_capability_auto_start_uses_runtime_state_graph(tmp_path: Path) -> None:
    provider = _PlanningProvider()
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
    )

    result = await registry.invoke(
        "goal.open",
        {
            "objective": "write app.py through runtime-backed goal capability",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["file.write", "test.run"],
            "verification_command": _command(
                "from pathlib import Path; assert Path('app.py').read_text() == 'agent\\n'"
            ),
            "timeout_seconds": 5,
        },
        permission="prepare",
        context=_context(tmp_path, trace_id="trace-goal-open"),
    )

    assert result.ok is True
    assert provider.calls == ["planner"]
    assert result.facts is not None
    assert result.facts["loop_terminal_state"] == LoopTerminalState.CONVERGED
    assert result.facts["completion_evidence"] is True
    evidence = result.facts["state_graph_result"]["evidence"]
    assert evidence["planned_capability"]["tool"] == "file.write"
    assert evidence["capability_result"]["ok"] is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "agent\n"
    decisions = [
        json.loads(event.output_json)
        for event in TraceStore(tmp_path).list_loop_decisions("trace-goal-open")
    ]
    transitions = [item for item in decisions if "condition" in item.get("evidence", {})]
    conditions = [item["evidence"]["condition"] for item in transitions]
    assert "plan_ready" in conditions
    assert "side_effect_recorded" in conditions
    assert transitions[-1]["decision"] == "converged"
    assert transitions[-1]["evidence"]["condition"] == "checker_passed"
    assert {
        item["evidence"]["loop_run_id"] for item in transitions
    } == {result.facts["loop_run_id"]}


@pytest.mark.asyncio
async def test_goal_open_capability_without_runtime_only_prepares_loop(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)

    result = await registry.invoke(
        "goal.open",
        {
            "objective": "prepare turn through durable state graph",
            "workspace": str(tmp_path),
            "loop_kind": "turn",
            "verification_command": _command("print('ok')"),
            "timeout_seconds": 5,
            "call_budget": 4,
            "token_budget": 1000,
            "cost_budget": 2.25,
            "qps_limit": 2,
            "max_concurrent": 3,
        },
        permission="prepare",
        context=_context(tmp_path),
    )

    assert result.ok is True
    assert result.facts is not None
    assert result.facts["completion_evidence"] is False
    assert result.facts["loop_terminal_state"] == ""
    assert result.facts["route"] == "unified_loop"
    assert result.facts["loop_kind"] == "turn"
    assert result.facts["budget_policy"]["call_budget"] == 4
    assert result.facts["budget_policy"]["token_budget"] == 1000
    assert result.facts["budget_policy"]["cost_budget"] == 2.25
    assert result.facts["budget_policy"]["qps_limit"] == 2
    assert result.facts["budget_policy"]["max_concurrent"] == 3

    run = RunStore(tmp_path).get(result.facts["run_id"])
    goal = GoalStore(tmp_path).get(result.facts["goal_id"])
    loop_run = LoopRunStore(tmp_path).get_run(result.facts["loop_run_id"])
    assert run is not None
    assert run.kind == "loop:turn"
    assert run.phase == Phase.RUNNING
    assert run.resolution == Resolution.NONE
    assert goal is not None
    assert goal.run_id == run.id
    assert loop_run is not None
    assert loop_run.terminal_state == ""


@pytest.mark.asyncio
async def test_goal_open_capability_can_create_goal_without_starting_loop(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)

    result = await registry.invoke(
        "goal.open",
        {
            "objective": "prepare unified loop only",
            "workspace": str(tmp_path),
            "verification_command": _command("print('ok')"),
            "auto_start": False,
        },
        permission="prepare",
        context=_context(tmp_path),
    )

    assert result.ok is True
    assert result.facts is not None
    assert result.facts["completion_evidence"] is False
    assert result.facts["loop_terminal_state"] == ""
    assert LoopRunStore(tmp_path).get_run(result.facts["loop_run_id"]) is not None


@pytest.mark.asyncio
async def test_goal_resume_capability_runs_checkpointed_goal(tmp_path: Path) -> None:
    provider = _PlanningProvider()
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
    )
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "resume through capability",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["file.write", "test.run"],
            "verification_command": _command(
                "from pathlib import Path; assert Path('app.py').read_text() == 'agent\\n'"
            ),
            "auto_start": False,
        },
        permission="prepare",
        context=_context(tmp_path),
    )

    resumed = await registry.invoke(
        "goal.resume",
        {"goal_id": opened.facts["goal_id"], "workspace": str(tmp_path)},
        permission="prepare",
        context=_context(tmp_path),
    )

    assert resumed.ok is True
    assert provider.calls == ["planner"]
    assert resumed.facts["state_transition"] == "resumed"
    assert resumed.facts["loop_run_id"] == opened.facts["loop_run_id"]
    assert resumed.facts["loop_terminal_state"] == LoopTerminalState.CONVERGED
    assert resumed.facts["resolution"] == Resolution.SUCCESS
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "agent\n"


@pytest.mark.asyncio
async def test_goal_cancel_capability_marks_loop_terminal(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "cancel through capability",
            "workspace": str(tmp_path),
            "verification_command": _command("print('ok')"),
            "auto_start": False,
        },
        permission="prepare",
        context=_context(tmp_path),
    )

    cancelled = await registry.invoke(
        "goal.cancel",
        {"goal_id": opened.facts["goal_id"], "reason": "user stop"},
        permission="prepare",
        context=_context(tmp_path),
    )

    assert cancelled.ok is True
    assert cancelled.facts["state_transition"] == "cancelled"
    assert cancelled.facts["loop_terminal_state"] == LoopTerminalState.CANCELLED
    assert cancelled.facts["resolution"] == Resolution.CANCELED


@pytest.mark.asyncio
async def test_goal_state_capability_reads_durable_loop_state(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "state through capability",
            "workspace": str(tmp_path),
            "verification_command": _command("print('ok')"),
            "auto_start": False,
        },
        permission="prepare",
        context=_context(tmp_path),
    )

    state = await registry.invoke(
        "goal.state",
        {"goal_id": opened.facts["goal_id"]},
        permission="read",
        context=_context(tmp_path),
    )

    assert state.ok is True
    assert state.facts["state_transition"] == "state_read"
    assert state.facts["goal"]["id"] == opened.facts["goal_id"]
    assert state.facts["loop_runs"][0]["run_id"] == opened.facts["loop_run_id"]
