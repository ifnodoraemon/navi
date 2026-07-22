from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.goals import GoalStore
from navi.lifecycle import Governance, Phase, Resolution
from navi.loop_control_service import LoopControlService, OpenGoalRequest
from navi.loop_contracts import LoopTerminalState
from navi.loop_runs import LoopRunStore
from navi.provider import ChatMessage
from navi.runtime import AgentRuntime
from navi.runs import RunStore
from navi.trace import TraceStore
from navi.workspaces import ShadowWorkspaceManager
from navi.actions.specs import ACTION_SPECS


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


async def _approve_capability_call(
    registry,
    home: Path,
    name: str,
    args: dict,
    *,
    context: CapabilityContext,
):
    suspended = await registry.invoke(name, args, permission="prepare", context=context)
    assert suspended.ok is False
    assert suspended.yields_control is True
    approval = RunStore(home).pending_approval_for_run(suspended.run_id)
    assert approval is not None
    resolved = await registry.invoke(
        "approval.resolve",
        {"decision": "approve", "code": approval.code},
        permission="prepare",
        context=context,
    )
    assert resolved.ok is True
    return await registry.invoke(name, args, permission="prepare", context=context)


def test_goal_open_description_is_factual_capability_metadata() -> None:
    spec = next(spec for spec in ACTION_SPECS if spec.name == "goal.open")

    assert "current capability and permission envelope" in spec.description
    assert "Use this when" not in spec.description
    assert "full system capabilities" not in spec.description


@pytest.mark.asyncio
async def test_respond_options_are_suggestions_not_user_pause(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)

    response = await registry.invoke(
        "respond",
        {"message": "I can list the details.", "options": ["list details"]},
        permission="read",
        context=_context(tmp_path),
    )
    question = await registry.invoke(
        "ask.user",
        {"message": "Which task should I cancel?", "options": ["first"]},
        permission="read",
        context=_context(tmp_path),
    )

    assert response.ok is True
    assert response.action == "chat"
    assert response.yields_control is False
    assert response.facts["options"] == ["list details"]
    assert question.ok is True
    assert question.action == "ask"
    assert question.yields_control is True


@pytest.mark.asyncio
async def test_respond_private_evidence_is_not_user_visible(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    token = "PRIVATE_SMOKE_TOKEN"

    response = await registry.invoke(
        "respond",
        {
            "message": "定时任务投递测试已完成，结果正文已按原文送达。",
            "private_evidence": {"smoke_token": token},
        },
        permission="read",
        context=_context(tmp_path),
    )

    assert response.ok is True
    assert response.action == "chat"
    assert response.message == "定时任务投递测试已完成，结果正文已按原文送达。"
    assert token not in response.message
    assert response.facts["private_evidence"] == {"smoke_token": token}
    assert response.facts["private_evidence_provenance"] == "respond.private_evidence"


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


@pytest.mark.asyncio
async def test_goal_open_scheduled_is_registration_not_immediate_execution(tmp_path: Path) -> None:
    provider = _PlanningProvider()
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
    )

    result = await registry.invoke(
        "goal.open",
        {
            "objective": "daily reminder",
            "workspace": str(tmp_path),
            "loop_kind": "scheduled",
            "cron_schedule": "15 8 * * *",
            "allowed_capabilities": ["respond"],
        },
        permission="prepare",
        context=_context(tmp_path),
    )

    assert result.ok is True
    assert provider.calls == []
    assert result.facts is not None
    assert result.facts["state_transition"] == "scheduled"
    assert result.facts["cron_schedule"] == "15 8 * * *"
    assert result.facts["registration_evidence"] is True
    assert result.facts["completion_evidence"] is True
    assert result.facts["loop_terminal_state"] == LoopTerminalState.CONVERGED
    assert LoopRunStore(tmp_path).list_active() == []


@pytest.mark.asyncio
async def test_goal_open_scheduled_persists_real_workspace_from_turn_shadow(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".navi"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("base\n", encoding="utf-8")
    shadow = ShadowWorkspaceManager(home).create_shadow(
        run_id="turn-register",
        workspace=repo,
    )
    registry = build_capability_registry(home, project_dir=repo)
    context = CapabilityContext(
        home=home,
        source="weixin",
        peer_id="peer-1",
        sender_id="user-1",
        permission_ceiling="write",
        workspace=shadow.shadow_workspace,
    )

    result = await registry.invoke(
        "goal.open",
        {
            "objective": "daily reminder",
            "loop_kind": "scheduled",
            "cron_schedule": "15 8 * * *",
            "allowed_capabilities": ["respond"],
        },
        permission="prepare",
        context=context,
    )

    assert result.ok is True
    registered = GoalStore(home).get(result.facts["goal_id"])
    assert registered is not None
    assert registered.workspace == str(repo.resolve())


@pytest.mark.asyncio
async def test_goal_open_cannot_expand_the_caller_workspace(tmp_path: Path) -> None:
    caller_workspace = tmp_path / "caller"
    outside_workspace = tmp_path / "outside"
    caller_workspace.mkdir()
    outside_workspace.mkdir()
    registry = build_capability_registry(tmp_path / ".navi", project_dir=caller_workspace)

    result = await registry.invoke(
        "goal.open",
        {
            "objective": "escape the caller workspace",
            "workspace": str(outside_workspace),
            "auto_start": False,
        },
        permission="prepare",
        context=CapabilityContext(
            home=tmp_path / ".navi",
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            permission_ceiling="write",
            workspace=str(caller_workspace),
        ),
    )

    assert result.ok is False
    assert result.error_reason == "permission_denied"
    assert GoalStore(tmp_path / ".navi").list(limit=10) == []


@pytest.mark.asyncio
async def test_goal_cancel_scope_accepts_shadow_for_same_durable_workspace(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".navi"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("base\n", encoding="utf-8")
    registry = build_capability_registry(home, project_dir=repo)
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "cancel from managed shadow",
            "workspace": str(repo),
            "auto_start": False,
        },
        permission="prepare",
        context=CapabilityContext(
            home=home,
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            permission_ceiling="write",
            workspace=str(repo),
        ),
    )
    shadow = ShadowWorkspaceManager(home).create_shadow(
        run_id="turn-cancel",
        workspace=repo,
    )

    cancel_args = {
        "goal_id": opened.facts["goal_id"],
        "reason": "user requested rebuild",
    }
    cancel_context = CapabilityContext(
            home=home,
            goal_id=opened.facts["goal_id"],
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            permission_ceiling="write",
            workspace=shadow.shadow_workspace,
        )
    cancelled = await _approve_capability_call(
        registry,
        home,
        "goal.cancel",
        cancel_args,
        context=cancel_context,
    )

    assert cancelled.ok is True
    assert cancelled.facts["state_transition"] == "cancelled"
    assert cancelled.facts["loop_terminal_state"] == LoopTerminalState.CANCELLED


@pytest.mark.asyncio
async def test_goal_cancel_scope_rejects_different_workspace_with_same_actor(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".navi"
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    registry = build_capability_registry(home, project_dir=repo_a)
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "workspace scoped task",
            "workspace": str(repo_a),
            "auto_start": False,
        },
        permission="prepare",
        context=CapabilityContext(
            home=home,
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            permission_ceiling="write",
            workspace=str(repo_a),
        ),
    )

    denied = await registry.invoke(
        "goal.cancel",
        {"goal_id": opened.facts["goal_id"], "reason": "wrong workspace"},
        permission="prepare",
        context=CapabilityContext(
            home=home,
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            permission_ceiling="write",
            workspace=str(repo_b),
        ),
    )

    assert denied.ok is False
    assert denied.error_reason == "permission_denied"


@pytest.mark.asyncio
async def test_goal_update_scheduled_template_reuses_goal_and_spec(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _context(tmp_path)
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "daily weather",
            "workspace": str(tmp_path),
            "loop_kind": "scheduled",
            "cron_schedule": "30 7 * * *",
            "allowed_capabilities": ["respond"],
            "acceptance_criteria": ["send the weather"],
        },
        permission="prepare",
        context=context,
    )

    updated = await registry.invoke(
        "goal.update",
        {
            "goal_id": opened.facts["goal_id"],
            "objective": "daily weather plus progressive AI hardware lesson",
            "acceptance_criteria": ["send weather and the next AI hardware lesson"],
        },
        permission="prepare",
        context=context,
    )

    assert updated.ok is True
    assert updated.facts["state_transition"] == "updated"
    assert updated.facts["goal_id"] == opened.facts["goal_id"]
    assert updated.facts["loop_spec_id"] == opened.facts["loop_spec_id"]
    goals = [
        goal
        for goal in GoalStore(tmp_path).list_cron_goals()
        if goal.phase != Phase.ENDED and goal.cron_schedule == "30 7 * * *"
    ]
    assert len(goals) == 1
    assert goals[0].objective == "daily weather plus progressive AI hardware lesson"
    spec = LoopControlService(tmp_path).goal_loop_spec(opened.facts["goal_id"])
    assert spec.goal.objective == "daily weather plus progressive AI hardware lesson"
    assert spec.goal.acceptance_criteria == ("send weather and the next AI hardware lesson",)


@pytest.mark.asyncio
async def test_goal_open_same_actor_same_cron_requires_update_or_duplicate_intent(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _context(tmp_path)
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "daily weather",
            "workspace": str(tmp_path),
            "loop_kind": "scheduled",
            "cron_schedule": "30 7 * * *",
            "allowed_capabilities": ["respond"],
        },
        permission="prepare",
        context=context,
    )

    conflict = await registry.invoke(
        "goal.open",
        {
            "objective": "daily weather plus hardware lesson",
            "workspace": str(tmp_path),
            "loop_kind": "scheduled",
            "cron_schedule": "30 7 * * *",
            "allowed_capabilities": ["respond"],
        },
        permission="prepare",
        context=context,
    )
    duplicate = await registry.invoke(
        "goal.open",
        {
            "objective": "independent daily hardware lesson",
            "workspace": str(tmp_path),
            "loop_kind": "scheduled",
            "cron_schedule": "30 7 * * *",
            "allowed_capabilities": ["respond"],
            "allow_duplicate_schedule": True,
        },
        permission="prepare",
        context=context,
    )

    assert opened.ok is True
    assert conflict.ok is False
    assert conflict.error_reason == "conflict"
    assert conflict.message == (
        "active scheduled goal already exists for this actor and cron_schedule."
    )
    assert "use goal.update" not in conflict.message
    assert "choose" not in conflict.message
    assert conflict.facts["reason"] == "active_actor_cron_schedule_conflict"
    assert conflict.facts["operation"] == "goal.open"
    assert conflict.facts["conflict_goal_id"] == opened.facts["goal_id"]
    assert conflict.facts["conflict_goal"]["cron_schedule"] == "30 7 * * *"
    assert duplicate.ok is True
    assert duplicate.facts["goal_id"] != opened.facts["goal_id"]


@pytest.mark.asyncio
async def test_goal_update_same_actor_same_cron_returns_conflict_facts(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = _context(tmp_path)
    morning = await registry.invoke(
        "goal.open",
        {
            "objective": "morning lesson",
            "workspace": str(tmp_path),
            "loop_kind": "scheduled",
            "cron_schedule": "30 7 * * *",
            "allowed_capabilities": ["respond"],
        },
        permission="prepare",
        context=context,
    )
    evening = await registry.invoke(
        "goal.open",
        {
            "objective": "evening lesson",
            "workspace": str(tmp_path),
            "loop_kind": "scheduled",
            "cron_schedule": "0 20 * * *",
            "allowed_capabilities": ["respond"],
        },
        permission="prepare",
        context=context,
    )

    conflict = await registry.invoke(
        "goal.update",
        {
            "goal_id": evening.facts["goal_id"],
            "cron_schedule": "30 7 * * *",
        },
        permission="prepare",
        context=context,
    )

    assert morning.ok is True
    assert evening.ok is True
    assert conflict.ok is False
    assert conflict.error_reason == "conflict"
    assert conflict.message == (
        "another active scheduled goal already exists for this actor and cron_schedule."
    )
    assert "choose" not in conflict.message
    assert "explicitly allow" not in conflict.message
    assert conflict.facts["reason"] == "active_actor_cron_schedule_conflict"
    assert conflict.facts["operation"] == "goal.update"
    assert conflict.facts["conflict_goal_id"] == morning.facts["goal_id"]
    assert conflict.facts["allow_duplicate_schedule"] is False


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
            "allowed_capabilities": ["file.write", "shell.run"],
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
    assert result.facts["execution_mode"] == "foreground"
    assert result.facts["loop_terminal_state"] == LoopTerminalState.WAITING_APPROVAL
    assert result.facts["completion_evidence"] is False
    evidence = result.facts["state_graph_result"]["evidence"]
    assert evidence["planned_capability"]["tool"] == "file.write"
    assert evidence["capability_result"]["yields_control"] is True
    assert "files" not in evidence["shadow_workspace"]["baseline_fingerprint"]
    assert evidence["shadow_workspace"]["baseline_fingerprint"]["file_count"] >= 0
    assert "evidence" not in result.facts["state_graph_result"]["run_state"]
    approval = RunStore(tmp_path).pending_approval_for_run(result.run_id)
    assert approval is not None
    RunStore(tmp_path).resolve_approval(
        approval.id,
        decision="approve",
        resolved_by="tester",
    )
    resumed = await _approve_capability_call(
        registry,
        tmp_path,
        "goal.resume",
        {"goal_id": result.facts["goal_id"], "workspace": str(tmp_path)},
        context=_context(tmp_path, trace_id="trace-goal-open"),
    )
    assert provider.calls == ["planner"]
    assert resumed.facts["loop_terminal_state"] == LoopTerminalState.CONVERGED
    assert resumed.facts["completion_evidence"] is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "agent\n"
    decisions = [
        json.loads(event.output_json)
        for event in TraceStore(tmp_path).list_loop_decisions("trace-goal-open")
    ]
    transitions = [item for item in decisions if "condition" in item.get("evidence", {})]
    conditions = [item["evidence"]["condition"] for item in transitions]
    assert "plan_ready" in conditions
    assert "approval_required" in conditions
    assert "side_effect_recorded" in conditions
    assert transitions[-1]["decision"] == "converged"
    assert transitions[-1]["evidence"]["condition"] == "checker_passed"
    assert {item["evidence"]["loop_run_id"] for item in transitions} == {
        resumed.facts["loop_run_id"]
    }


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
    assert result.facts["execution_mode"] == "background"
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
    assert result.facts["execution_mode"] == "manual"
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
            "allowed_capabilities": ["file.write", "shell.run"],
            "verification_command": _command(
                "from pathlib import Path; assert Path('app.py').read_text() == 'agent\\n'"
            ),
            "auto_start": False,
        },
        permission="prepare",
        context=_context(tmp_path),
    )

    resumed = await _approve_capability_call(
        registry,
        tmp_path,
        "goal.resume",
        {"goal_id": opened.facts["goal_id"], "workspace": str(tmp_path)},
        context=_context(tmp_path),
    )

    assert resumed.ok is True
    assert provider.calls == ["planner"]
    assert resumed.facts["state_transition"] == "resumed"
    assert resumed.facts["loop_run_id"] == opened.facts["loop_run_id"]
    assert resumed.facts["loop_terminal_state"] == LoopTerminalState.WAITING_APPROVAL
    approval = RunStore(tmp_path).pending_approval_for_run(resumed.run_id)
    assert approval is not None
    RunStore(tmp_path).resolve_approval(
        approval.id,
        decision="approve",
        resolved_by="tester",
    )
    completed = await registry.invoke(
        "goal.resume",
        {"goal_id": opened.facts["goal_id"], "workspace": str(tmp_path)},
        permission="prepare",
        context=_context(tmp_path),
    )
    assert provider.calls == ["planner"]
    assert completed.facts["loop_terminal_state"] == LoopTerminalState.CONVERGED
    assert completed.facts["resolution"] == Resolution.SUCCESS
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

    cancelled = await _approve_capability_call(
        registry,
        tmp_path,
        "goal.cancel",
        {"goal_id": opened.facts["goal_id"], "reason": "user stop"},
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


@pytest.mark.asyncio
async def test_goal_state_default_and_explicit_reads_are_caller_scoped(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    visible = await registry.invoke(
        "goal.open",
        {
            "objective": "visible current task",
            "workspace": str(tmp_path),
            "auto_start": False,
        },
        permission="prepare",
        context=_context(tmp_path),
    )
    hidden = LoopControlService(tmp_path).open_goal(
        OpenGoalRequest(
            objective="other actor task",
            workspace=str(tmp_path),
            source="telegram",
            peer_id="other-peer",
            sender_id="other-user",
            auto_start=False,
        )
    )

    scoped = await registry.invoke(
        "goal.state",
        {},
        permission="read",
        context=_context(tmp_path),
    )
    denied = await registry.invoke(
        "goal.state",
        {"goal_id": hidden.goal.id},
        permission="read",
        context=_context(tmp_path),
    )

    assert scoped.ok is True
    assert "active_goals" not in scoped.facts
    assert [goal["id"] for goal in scoped.facts["current_goals"]] == [visible.facts["goal_id"]]
    assert denied.ok is False
    assert denied.error_reason == "permission_denied"


@pytest.mark.asyncio
async def test_goal_state_scoped_view_omits_raw_evidence_json(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    opened = await registry.invoke(
        "goal.open",
        {
            "objective": "visible current task with evidence",
            "workspace": str(tmp_path),
            "auto_start": False,
        },
        permission="prepare",
        context=_context(tmp_path),
    )

    explicit = await registry.invoke(
        "goal.state",
        {"goal_id": opened.facts["goal_id"]},
        permission="read",
        context=_context(tmp_path),
    )
    scoped = await registry.invoke(
        "goal.state",
        {},
        permission="read",
        context=_context(tmp_path),
    )

    assert explicit.ok is True
    assert "evidence_json" in explicit.facts["goal"]
    assert "evidence" in explicit.facts["loop_runs"][0]
    assert scoped.ok is True
    assert scoped.facts["current_goals"]
    assert scoped.facts["active_loop_runs"]
    assert "evidence_json" not in scoped.facts["goals"][0]
    assert "evidence_json" not in scoped.facts["current_goals"][0]
    assert "evidence_json" not in scoped.facts["active_loop_runs"][0]


@pytest.mark.asyncio
async def test_goal_state_scheduled_view_is_actor_scoped_and_authoritative(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    visible = await registry.invoke(
        "goal.open",
        {
            "objective": "visible daily schedule",
            "workspace": str(tmp_path),
            "loop_kind": "scheduled",
            "cron_schedule": "15 8 * * *",
            "allowed_capabilities": ["respond"],
        },
        permission="prepare",
        context=_context(tmp_path),
    )
    LoopControlService(tmp_path).open_goal(
        OpenGoalRequest(
            objective="other actor schedule",
            workspace=str(tmp_path),
            loop_kind="scheduled",
            cron_schedule="30 7 * * *",
            source="telegram",
            peer_id="other-peer",
            sender_id="other-user",
            allowed_capabilities=("respond",),
        )
    )

    state = await registry.invoke(
        "goal.state",
        {"view": "scheduled"},
        permission="read",
        context=_context(tmp_path),
    )

    assert state.ok is True
    assert state.facts["authoritative_for"] == "actor_scheduled_goals"
    assert state.facts["matched_count"] == 1
    assert [goal["id"] for goal in state.facts["scheduled_goals"]] == [visible.facts["goal_id"]]
    assert "active_goals" not in state.facts


@pytest.mark.asyncio
async def test_goal_state_actor_scope_is_applied_before_limit(tmp_path: Path) -> None:
    visible = GoalStore(tmp_path).create(
        objective="visible older task",
        workspace=str(tmp_path),
        source="cli",
        peer_id="cli",
        sender_id="tester",
    )
    for index in range(101):
        GoalStore(tmp_path).create(
            objective=f"hidden newer task {index}",
            workspace=str(tmp_path),
            source="telegram",
            peer_id="other-peer",
            sender_id="other-user",
        )

    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    state = await registry.invoke(
        "goal.state",
        {"view": "history", "limit": 20},
        permission="read",
        context=_context(tmp_path),
    )

    assert state.ok is True
    assert state.facts["authoritative_for"] == "actor_goal_history"
    assert state.facts["matched_count"] == 1
    assert [goal["id"] for goal in state.facts["history_goals"]] == [visible.id]


@pytest.mark.asyncio
async def test_goal_state_pending_approval_view_is_explicit(tmp_path: Path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    pending = await registry.invoke(
        "goal.open",
        {
            "objective": "delete report after approval",
            "workspace": str(tmp_path),
            "auto_start": False,
        },
        permission="prepare",
        context=_context(tmp_path),
    )
    done = await registry.invoke(
        "goal.open",
        {
            "objective": "ordinary active work",
            "workspace": str(tmp_path),
            "auto_start": False,
        },
        permission="prepare",
        context=_context(tmp_path),
    )
    store = GoalStore(tmp_path)
    store.update_state(
        pending.facts["goal_id"],
        governance=Governance.AWAITING_APPROVAL,
        task_status="pending",
    )

    state = await registry.invoke(
        "goal.state",
        {"view": "pending_approval"},
        permission="read",
        context=_context(tmp_path),
    )

    assert state.ok is True
    assert state.facts["authoritative_for"] == "current_actor_pending_approval_goals"
    assert state.facts["goal_counts"]["pending_approval"] == 1
    assert [goal["id"] for goal in state.facts["pending_approval_goals"]] == [
        pending.facts["goal_id"]
    ]
    assert done.facts["goal_id"] not in {
        goal["id"] for goal in state.facts["pending_approval_goals"]
    }
    assert "active_goals" not in state.facts


@pytest.mark.asyncio
async def test_goal_cancel_explicit_pending_approval_ids_cancel_and_verify(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    opened_ids: list[str] = []
    for objective in ("pending file write", "pending shell delete"):
        opened = await registry.invoke(
            "goal.open",
            {
                "objective": objective,
                "workspace": str(tmp_path),
                "auto_start": False,
            },
            permission="prepare",
            context=_context(tmp_path),
        )
        opened_ids.append(opened.facts["goal_id"])
    store = GoalStore(tmp_path)
    for goal_id in opened_ids:
        store.update_state(
            goal_id,
            governance=Governance.AWAITING_APPROVAL,
            task_status="pending",
        )

    state = await registry.invoke(
        "goal.state",
        {"view": "pending_approval"},
        permission="read",
        context=_context(tmp_path),
    )
    pending_ids = [goal["id"] for goal in state.facts["pending_approval_goals"]]

    cancelled = await _approve_capability_call(
        registry,
        tmp_path,
        "goal.cancel",
        {"goal_ids": pending_ids, "reason": "user requested cleanup"},
        context=_context(tmp_path),
    )

    assert cancelled.ok is True
    assert cancelled.facts["requested_count"] == 2
    assert cancelled.facts["cancelled_count"] == 2
    assert cancelled.facts["failed_count"] == 0
    assert set(cancelled.facts["verified_after"]["cancelled_goal_ids"]) == set(opened_ids)
    assert {item["goal_id"] for item in cancelled.facts["cancelled_goals"]} == set(opened_ids)
    for item in cancelled.facts["cancelled_goals"]:
        assert item["verified_goal"]["phase"] == Phase.ENDED
        assert item["verified_goal"]["resolution"] == Resolution.CANCELED
