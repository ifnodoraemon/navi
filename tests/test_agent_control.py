from __future__ import annotations

import json
from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.daemon import SystemDaemon
from navi.goals import GoalStore
from navi.goal_state_graph import resume_goal_loop_run
from navi.loop_control_service import LoopControlService, OpenGoalRequest
from navi.loop_contracts import LoopTerminalState
from navi.loop_runs import LoopRunStore
from navi.runtime import AgentRuntime


class _ChildReportProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete_for(self, role: str, messages, **kwargs) -> str:
        del messages, kwargs
        self.calls.append(role)
        if role == "planner":
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "agent.report",
                            "permission": "prepare",
                            "args": {
                                "summary": "background child completed",
                                "findings": [{"kind": "fact", "value": "verified"}],
                                "evidence_refs": ["daemon-smoke"],
                            },
                            "reason": "return the bounded result to the parent",
                        }
                    ]
                }
            )
        if role == "checker":
            return json.dumps(
                {
                    "passed": True,
                    "should_continue": False,
                    "evidence_summary": "child report satisfies acceptance criteria",
                }
            )
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "checker", "responder"]

    def usage_for(self, role: str) -> dict:
        del role
        return {}


def _context(
    home: Path,
    *,
    goal_id: str = "",
    loop_run_id: str = "",
) -> CapabilityContext:
    return CapabilityContext(
        home=home,
        goal_id=goal_id,
        loop_run_id=loop_run_id,
        source="weixin",
        peer_id="peer-1",
        sender_id="user-1",
        session_id="session-1",
        workspace=str(home),
        permission_ceiling="write",
    )


def _parent(home: Path):
    return LoopControlService(home).open_goal(
        OpenGoalRequest(
            objective="coordinate bounded research",
            workspace=str(home),
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            session_id="session-1",
            permission_ceiling="write",
            allowed_capabilities=("agent.control", "shell.run"),
            auto_start=False,
            execution_mode="foreground",
        )
    )


def _spawn_args(parent_goal_id: str, *, objective: str = "inspect repository") -> dict:
    return {
        "operation": "spawn",
        "parent_goal_id": parent_goal_id,
        "objective": objective,
        "acceptance_criteria": ["return evidence-backed findings"],
        "context_facts": {"requested_by": "parent"},
        "allowed_capabilities": ["shell.run"],
        "call_budget": 4,
    }


@pytest.mark.asyncio
async def test_agent_control_uses_one_parent_surface_and_child_only_report(tmp_path: Path) -> None:
    parent = _parent(tmp_path)
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    parent_context = _context(
        tmp_path,
        goal_id=parent.goal.id,
        loop_run_id=parent.loop_run.run_id,
    )

    names = {spec.name for spec in registry.list_specs()}
    assert {"agent.control", "agent.report"} <= names
    assert (
        not {
            "agent.spawn",
            "agent.list",
            "agent.state",
            "agent.message",
            "agent.cancel",
            "agent.collect",
        }
        & names
    )

    insufficient = await registry.invoke(
        "agent.control",
        _spawn_args(parent.goal.id),
        permission="read",
        context=parent_context,
    )
    assert insufficient.ok is False
    assert insufficient.error_reason == "permission_escalation"

    spawned = await registry.invoke(
        "agent.control",
        _spawn_args(parent.goal.id),
        permission="prepare",
        context=parent_context,
    )
    assert spawned.ok is True
    child_id = spawned.facts["child_goal_id"]
    child_loop_id = spawned.facts["loop_run_id"]
    child = GoalStore(tmp_path).get(child_id)
    assert child is not None
    assert child.parent_goal_id == parent.goal.id
    assert spawned.facts["allowed_capabilities"] == ["agent.report", "shell.run"]
    assert spawned.facts["permission_ceiling"] == "prepare"
    assert 1 <= spawned.facts["timeout_seconds"] <= 120

    messaged = await registry.invoke(
        "agent.control",
        {
            "operation": "message",
            "parent_goal_id": parent.goal.id,
            "child_goal_id": child_id,
            "message": "also inspect the runtime contract",
            "facts": {"priority": "P0"},
        },
        permission="prepare",
        context=parent_context,
    )
    assert messaged.ok is True
    assert any(
        event.event_type == "agent.message_received"
        for event in GoalStore(tmp_path).list_events(child_id)
    )

    child_context = _context(
        tmp_path,
        goal_id=child_id,
        loop_run_id=child_loop_id,
    )
    reported = await registry.invoke(
        "agent.report",
        {
            "summary": "repository contract inspected",
            "findings": [{"kind": "fact", "value": "bounded"}],
            "evidence_refs": ["src/navi/actions/agent.py"],
        },
        permission="prepare",
        context=child_context,
    )
    assert reported.ok is True
    assert reported.terminal is True
    assert reported.facts["parent_goal_id"] == parent.goal.id

    collected = await registry.invoke(
        "agent.control",
        {
            "operation": "collect",
            "parent_goal_id": parent.goal.id,
            "child_goal_id": child_id,
        },
        permission="read",
        context=parent_context,
    )
    assert collected.ok is True
    assert collected.facts["latest_report"]["summary"] == ("repository contract inspected")
    assert collected.facts["completion_evidence"] is False


@pytest.mark.asyncio
async def test_agent_control_enforces_depth_identity_capability_and_concurrency(
    tmp_path: Path,
) -> None:
    parent = _parent(tmp_path)
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    parent_context = _context(tmp_path, goal_id=parent.goal.id)

    outside_envelope = await registry.invoke(
        "agent.control",
        {
            **_spawn_args(parent.goal.id),
            "allowed_capabilities": ["file.write"],
        },
        permission="prepare",
        context=parent_context,
    )
    assert outside_envelope.ok is False
    assert outside_envelope.error_reason == "schema_mismatch"

    children = []
    for index in range(3):
        result = await registry.invoke(
            "agent.control",
            _spawn_args(parent.goal.id, objective=f"child task {index}"),
            permission="prepare",
            context=parent_context,
        )
        assert result.ok is True
        children.append(result.facts["child_goal_id"])

    fourth = await registry.invoke(
        "agent.control",
        _spawn_args(parent.goal.id, objective="fourth child"),
        permission="prepare",
        context=parent_context,
    )
    assert fourth.ok is False
    assert fourth.error_reason == "conflict"

    child_context = _context(tmp_path, goal_id=children[0])
    recursive = await registry.invoke(
        "agent.control",
        _spawn_args(children[0], objective="recursive child"),
        permission="prepare",
        context=child_context,
    )
    assert recursive.ok is False
    assert recursive.error_reason == "permission_denied"

    other_actor = CapabilityContext(
        **{
            **parent_context.__dict__,
            "sender_id": "other-user",
        }
    )
    leaked = await registry.invoke(
        "agent.control",
        {
            "operation": "state",
            "parent_goal_id": parent.goal.id,
            "child_goal_id": children[0],
        },
        permission="read",
        context=other_actor,
    )
    assert leaked.ok is False
    assert leaked.error_reason == "permission_denied"


@pytest.mark.asyncio
async def test_agent_spawn_uses_atomic_child_admission_when_action_count_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent(tmp_path)
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    parent_context = _context(tmp_path, goal_id=parent.goal.id)

    for index in range(3):
        result = await registry.invoke(
            "agent.control",
            _spawn_args(parent.goal.id, objective=f"child task {index}"),
            permission="prepare",
            context=parent_context,
        )
        assert result.ok is True

    monkeypatch.setattr(GoalStore, "count_children", lambda *args, **kwargs: 0)
    fourth = await registry.invoke(
        "agent.control",
        _spawn_args(parent.goal.id, objective="fourth child from stale gate"),
        permission="prepare",
        context=parent_context,
    )

    assert fourth.ok is False
    assert fourth.error_reason == "conflict"
    actual_children = GoalStore(tmp_path).list_children(parent.goal.id, limit=10)
    assert len([child for child in actual_children if child.phase != "ended"]) == 3


@pytest.mark.asyncio
async def test_daemon_resumes_background_child_and_parent_collects_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent(tmp_path)
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    parent_context = _context(tmp_path, goal_id=parent.goal.id)
    spawned = await registry.invoke(
        "agent.control",
        _spawn_args(parent.goal.id),
        permission="prepare",
        context=parent_context,
    )
    child_id = spawned.facts["child_goal_id"]

    provider = _ChildReportProvider()
    monkeypatch.setattr("navi.provider.build_provider", lambda _config: provider)
    processed = await SystemDaemon(tmp_path, project_dir=tmp_path).process_queue_once()

    assert len(processed) == 1
    assert provider.calls == ["planner", "checker"]
    child = GoalStore(tmp_path).get(child_id)
    assert child is not None
    assert str(child.phase) == "ended"
    collected = await registry.invoke(
        "agent.control",
        {
            "operation": "collect",
            "parent_goal_id": parent.goal.id,
            "child_goal_id": child_id,
        },
        permission="read",
        context=parent_context,
    )
    assert collected.ok is True
    assert collected.facts["child"]["loop_terminal_state"] == (LoopTerminalState.CONVERGED)
    assert collected.facts["completion_evidence"] is True
    assert collected.facts["latest_report"]["summary"] == ("background child completed")


@pytest.mark.asyncio
async def test_background_child_retries_transient_resource_pauses_at_original_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent(tmp_path)
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    parent_context = _context(tmp_path, goal_id=parent.goal.id)
    spawned = await registry.invoke(
        "agent.control",
        {**_spawn_args(parent.goal.id), "qps_limit": 1},
        permission="prepare",
        context=parent_context,
    )
    loop_run_id = spawned.facts["loop_run_id"]
    provider = _ChildReportProvider()
    monkeypatch.setattr("navi.provider.build_provider", lambda _config: provider)
    monkeypatch.setattr("navi.resource_gateway.time.time", lambda: 1000.0)

    first = await SystemDaemon(tmp_path, project_dir=tmp_path).process_queue_once()
    first_state = LoopRunStore(tmp_path).get_run(loop_run_id)
    assert len(first) == 1
    assert first_state is not None
    assert first_state.terminal_state == LoopTerminalState.PAUSED
    assert first_state.evidence["resource_resume_node"] == "execute"
    assert (
        LoopRunStore(tmp_path).list_retryable_background_pauses(now=float("inf"))[0].run_id
        == loop_run_id
    )

    runtime = AgentRuntime(home=tmp_path, provider=provider)
    second = await resume_goal_loop_run(
        home=tmp_path,
        loop_run_id=loop_run_id,
        runtime=runtime,
        entrypoint="test.resource_retry",
        resume_reason="transient_resource_retry",
        state_transition="resource_retried",
        resource_retry=True,
    )
    assert second.loop_run.terminal_state == LoopTerminalState.PAUSED
    assert second.loop_run.evidence["resource_resume_node"] == "evaluate"

    completed = await resume_goal_loop_run(
        home=tmp_path,
        loop_run_id=loop_run_id,
        runtime=runtime,
        entrypoint="test.resource_retry",
        resume_reason="transient_resource_retry",
        state_transition="resource_retried",
        resource_retry=True,
    )
    assert completed.loop_run.terminal_state == LoopTerminalState.CONVERGED
    assert provider.calls == ["planner", "checker"]
    reports = [
        event
        for event in GoalStore(tmp_path).list_events(spawned.facts["child_goal_id"])
        if event.event_type == "agent.reported"
    ]
    assert len(reports) == 1
