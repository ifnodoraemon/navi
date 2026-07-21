"""Current capability surface and durable lifecycle integration checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.goals import GoalStore
from navi.tools import API_CONTEXT


def _context(home: Path) -> CapabilityContext:
    return CapabilityContext(
        home=home,
        peer_id="e2e",
        sender_id="e2e",
        source="cli",
        workspace=str(home),
    )


def test_t1_capability_registry_matches_current_control_surface(navi_home: Path) -> None:
    registry = build_capability_registry(navi_home, project_dir=Path.cwd())
    names = {spec.name for spec in registry.list_specs()}
    planner_names = {spec.name for spec in registry.planner_specs()}
    expected = {
        "respond",
        "agent.control",
        "agent.report",
        "approval.request",
        "approval.resolve",
        "goal.open",
        "goal.resume",
        "goal.cancel",
        "goal.state",
        "memory.recall",
        "tools.list",
        "workspace.shadow.create",
        "workspace.shadow.merge",
        "workspace.shadow.discard",
    }

    assert expected <= names
    assert expected <= planner_names
    assert {"delegate.spawn", "watch.create", "workflow.run"}.isdisjoint(names)


def test_t1_governed_evolution_proposal_is_visible_but_apply_stays_api_only(
    navi_home: Path,
) -> None:
    turn_registry = build_capability_registry(navi_home, project_dir=Path.cwd())
    api_registry = build_capability_registry(
        navi_home,
        project_dir=Path.cwd(),
        execution_context=API_CONTEXT,
    )
    turn_names = {spec.name for spec in turn_registry.list_specs()}
    api_names = {spec.name for spec in api_registry.list_specs()}
    api_only = {
        "session.create",
        "trace.evaluate",
        "evolution.record_evaluation",
        "evolution.apply",
        "evolution.rollback",
    }

    assert api_only <= api_names
    assert api_only.isdisjoint(turn_names)
    assert "evolution.propose" in api_names
    assert "evolution.propose" in turn_names


@pytest.mark.asyncio
async def test_t1_conversation_actions_dispatch(navi_home: Path) -> None:
    registry = build_capability_registry(navi_home, project_dir=Path.cwd())
    context = _context(navi_home)

    final = await registry.invoke(
        "respond",
        {"message": "Hello Final E2E"},
        permission="read",
        context=context,
    )
    question = await registry.invoke(
        "ask.user",
        {"message": "Please select", "options": ["optionA", "optionB"]},
        permission="read",
        context=context,
    )

    assert final.ok is True and final.action == "chat" and final.terminal is True
    assert final.message == "Hello Final E2E"
    assert question.ok is True and question.action == "ask" and question.terminal is True
    assert question.facts == {"options": ["optionA", "optionB"]}


@pytest.mark.asyncio
async def test_t1_scheduled_goal_lifecycle_uses_goal_control(navi_home: Path) -> None:
    registry = build_capability_registry(navi_home, project_dir=Path.cwd())
    context = _context(navi_home)
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "E2E scheduled reminder",
            "workspace": str(navi_home),
            "loop_kind": "scheduled",
            "cron_schedule": "*/5 * * * *",
            "allowed_capabilities": ["respond"],
        },
        permission="prepare",
        context=context,
    )
    state = await registry.invoke(
        "goal.state",
        {"goal_id": opened.facts["goal_id"]},
        permission="read",
        context=context,
    )
    cancelled = await registry.invoke(
        "goal.cancel",
        {"goal_id": opened.facts["goal_id"], "reason": "E2E cleanup"},
        permission="prepare",
        context=context,
    )

    assert opened.ok is True
    assert opened.facts["state_transition"] == "scheduled"
    assert state.ok is True and state.facts["goal"]["cron_schedule"] == "*/5 * * * *"
    assert cancelled.ok is True
    assert cancelled.facts["resolution"] == "canceled"
    goal = GoalStore(navi_home).get(opened.facts["goal_id"])
    assert goal is not None and goal.phase == "ended"
    assert GoalStore(navi_home).due_cron_goals(float("inf")) == []
