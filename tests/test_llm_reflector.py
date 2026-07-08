"""Tests for the agentic LLM reflector in the durable StateGraph.

These tests verify the "never give up" loop: when a goal has no required
verification command, the LLM reflector (not a deterministic rule) judges
whether the objective is achieved and decides whether to retry with a new
plan, converge, or block with a user message.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.lifecycle import Resolution
from navi.loop_contracts import LoopTerminalState
from navi.loop_control_service import LoopControlService, OpenGoalRequest
from navi.loop_runs import LoopRunStore
from navi.provider import ChatMessage
from navi.runtime import AgentRuntime


class _ScriptedProvider:
    """A provider that returns scripted responses per role.

    - planner: returns a single syscall (the next capability to try)
    - reflector: returns the LLM's judgement of whether the goal is achieved
    - responder: returns a natural language reply synthesized from facts
    """

    def __init__(
        self,
        *,
        planner_syscalls: list[dict],
        reflector_decisions: list[dict],
    ) -> None:
        self._planner_syscalls = list(planner_syscalls)
        self._reflector_decisions = list(reflector_decisions)
        self.calls: list[str] = []

    async def complete_for(
        self, role: str, messages: list[ChatMessage], **kwargs
    ) -> str:
        self.calls.append(role)
        if role == "planner":
            syscall = self._planner_syscalls.pop(0)
            return json.dumps({"syscalls": [syscall]})
        if role == "reflector":
            decision = self._reflector_decisions.pop(0)
            return json.dumps(decision)
        # responder / default — synthesize from the facts payload
        return "I'll handle that for you."

    def list_roles(self) -> list[str]:
        return ["planner", "reflector", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


def _context(home: Path) -> CapabilityContext:
    return CapabilityContext(
        home=home,
        source="cli",
        peer_id="cli",
        sender_id="tester",
        permission_ceiling="write",
        workspace=str(home),
    )


def _file_write_syscall(path: str, content: str) -> dict:
    return {
        "tool": "file.write",
        "permission": "write",
        "args": {
            "path": path,
            "content": content,
            "mode": "overwrite",
            "create_dirs": True,
        },
        "model_role": "executor",
        "reason": f"write {path}",
    }


@pytest.mark.asyncio
async def test_llm_reflector_converges_when_goal_achieved(tmp_path: Path) -> None:
    """The LLM reflector judges goal_achieved=true and converges."""
    provider = _ScriptedProvider(
        planner_syscalls=[_file_write_syscall("done.txt", "ok")],
        reflector_decisions=[
            {
                "goal_achieved": True,
                "should_continue": False,
                "next_step_hint": "file written",
                "user_message": "Done — wrote done.txt.",
            }
        ],
    )
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=runtime,
    )

    result = await registry.invoke(
        "goal.open",
        {
            "objective": "write done.txt",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["file.write"],
            "verification_command": "",
        },
        permission="prepare",
        context=_context(tmp_path),
    )

    assert result.ok is True
    assert result.facts["loop_terminal_state"] == LoopTerminalState.CONVERGED
    assert (tmp_path / "done.txt").read_text(encoding="utf-8") == "ok"
    assert "reflector" in provider.calls


@pytest.mark.asyncio
async def test_llm_reflector_retries_then_converges(tmp_path: Path) -> None:
    """The LLM reflector says should_continue=true, then converges on attempt 2."""
    provider = _ScriptedProvider(
        planner_syscalls=[
            _file_write_syscall("v1.txt", "first"),
            _file_write_syscall("v2.txt", "second"),
        ],
        reflector_decisions=[
            {
                "goal_achieved": False,
                "should_continue": True,
                "next_step_hint": "try writing v2.txt instead",
                "user_message": "Let me try a different file.",
            },
            {
                "goal_achieved": True,
                "should_continue": False,
                "next_step_hint": "v2.txt written",
                "user_message": "Done — wrote v2.txt.",
            },
        ],
    )
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=runtime,
    )

    result = await registry.invoke(
        "goal.open",
        {
            "objective": "write a file then verify",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["file.write"],
            "verification_command": "",
        },
        permission="prepare",
        context=_context(tmp_path),
    )

    assert result.ok is True
    assert result.facts["loop_terminal_state"] == LoopTerminalState.CONVERGED
    # both planner calls happened (2 iterations)
    assert provider.calls.count("planner") == 2
    assert provider.calls.count("reflector") == 2
    assert (tmp_path / "v2.txt").read_text(encoding="utf-8") == "second"


@pytest.mark.asyncio
async def test_llm_reflector_blocks_when_should_continue_false(tmp_path: Path) -> None:
    """When the LLM says should_continue=false and goal not achieved, block."""
    provider = _ScriptedProvider(
        planner_syscalls=[_file_write_syscall("v1.txt", "first")],
        reflector_decisions=[
            {
                "goal_achieved": False,
                "should_continue": False,
                "next_step_hint": "cannot proceed without more info",
                "user_message": "I need more details to complete this.",
            }
        ],
    )
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=runtime,
    )

    result = await registry.invoke(
        "goal.open",
        {
            "objective": "write a file then verify",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["file.write"],
            "verification_command": "",
        },
        permission="prepare",
        context=_context(tmp_path),
    )

    assert result.ok is True
    assert result.facts["loop_terminal_state"] == LoopTerminalState.BLOCKED
    assert result.facts["resolution"] == Resolution.BLOCKED


@pytest.mark.asyncio
async def test_llm_reflector_terminates_at_max_attempts(tmp_path: Path) -> None:
    """The loop terminates at max_attempts even if the LLM keeps saying continue.

    The scripted provider has 12 planner syscalls and 12 reflector decisions
    (more than the default max_attempts=10). If the loop did NOT terminate at
    max_attempts, it would exhaust the scripted responses and raise IndexError.
    So this test both verifies termination AND that the loop doesn't spin
    forever on a "should_continue=true" LLM.
    """
    continue_decision = {
        "goal_achieved": False,
        "should_continue": True,
        "next_step_hint": "keep trying",
        "user_message": "Still working on it.",
    }
    provider = _ScriptedProvider(
        planner_syscalls=[_file_write_syscall(f"v{i}.txt", f"iter-{i}") for i in range(12)],
        reflector_decisions=[continue_decision] * 12,
    )
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=runtime,
    )

    result = await registry.invoke(
        "goal.open",
        {
            "objective": "write a file then verify",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["file.write"],
            "verification_command": "",
            "timeout_seconds": 5,
        },
        permission="prepare",
        context=_context(tmp_path),
    )

    # The loop must terminate (not hang forever) — either BLOCKED or FAILED
    terminal = result.facts["loop_terminal_state"]
    assert terminal in (
        LoopTerminalState.BLOCKED,
        LoopTerminalState.FAILED,
    ), f"expected terminal state, got {terminal}"


def test_continue_iteration_edge_exists() -> None:
    """The EVALUATE -> PLAN [continue_iteration] edge must be registered."""
    from navi.loop_contracts import default_state_graph

    edges = default_state_graph()
    has_continue = any(
        str(e.source) == "evaluate"
        and str(e.target) == "plan"
        and e.condition == "continue_iteration"
        for e in edges
    )
    assert has_continue, "EVALUATE -> PLAN [continue_iteration] edge missing"


def test_evaluate_to_plan_increments_attempt() -> None:
    """EVALUATE -> PLAN iteration must increment attempt (anti-infinite-loop)."""
    from navi.loop_contracts import LoopNode, LoopRunState

    state = LoopRunState(
        run_id="r1",
        goal_id="g1",
        loop_spec_id="s1",
        node=LoopNode.EVALUATE,
        attempt=1,
    )
    next_state = state.transition(node=LoopNode.PLAN, checkpoint_id="c1")
    assert next_state.attempt == 2, (
        f"EVALUATE->PLAN must increment attempt, got {next_state.attempt}"
    )
