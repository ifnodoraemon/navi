from __future__ import annotations

import json
import re

import pytest

from navi.capabilities import CapabilityRegistry
from navi.control import CurrentStateBuilder, SurfaceContext, current_state_facts
from navi.control_plane import TurnController
from navi.goals import GoalStore
from navi.loop_contracts import (
    GoalSpec,
    LoopSpec,
    TimeoutPolicy,
    VerificationKind,
    VerificationStep,
)
from navi.loop_runs import LoopRunStore
from navi.provider import ChatMessage
from navi.runtime import AgentRuntime
from navi.state_graph import ModelCapabilityPlannerPort
from navi.workspaces import ShadowWorkspaceManager


class _CapturingPlannerProvider:
    def __init__(self) -> None:
        self.messages: list[ChatMessage] = []

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        **kwargs,
    ) -> str:
        self.messages = messages
        return json.dumps(
            {
                "syscalls": [
                    {
                        "tool": "respond",
                        "permission": "read",
                        "args": {"message": "I need one more fact."},
                        "model_role": "executor",
                        "reason": "ask from current facts",
                    }
                ]
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "executor", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


def _loop_spec(goal_id: str, workspace: str) -> LoopSpec:
    return LoopSpec.from_goal(
        GoalSpec(
            objective="Move Navi toward durable loop execution",
            scope=(f"repo:{workspace}",),
            constraints=("state graph decisions read CurrentState",),
            acceptance_criteria=("current state includes active loop runs",),
            permission_ceiling="write",
        ),
        goal_id=goal_id,
        allowed_capabilities=("filesystem.write", "test.run"),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.COMMAND_EXIT_CODE,
                name="tests",
                command="pytest -q",
                timeout=TimeoutPolicy(seconds=120),
            ),
        ),
    )


def test_current_state_facts_include_active_goal_and_loop_run_state(tmp_path):
    goal = GoalStore(tmp_path).create(
        objective="durable loop execution",
        workspace=str(tmp_path),
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        session_id="session-1",
        task_status="in_progress",
    )
    spec = _loop_spec(goal.id, str(tmp_path))
    loop_run = LoopRunStore(tmp_path).create_run(spec)

    state = CurrentStateBuilder(tmp_path).build(
        SurfaceContext(
            home=tmp_path,
            source="weixin",
            peer_id="peer-1",
            sender_id="sender-1",
            session_id="session-1",
            workspace=str(tmp_path),
        )
    )
    facts = current_state_facts(state)

    assert facts["active_goals"][0]["id"] == goal.id
    assert facts["goal_state"]["active_goals"][0]["objective"] == "durable loop execution"
    assert facts["goal_state"]["active_goals"][0]["task_status"] == "in_progress"
    assert facts["active_loop_runs"][0]["run_id"] == loop_run.run_id
    assert facts["loop_run_state"]["active_loop_runs"][0]["loop_spec_id"] == spec.id
    assert facts["budget_state"]["decision"] == "allow"
    assert facts["workspace_state"]["workspace"] == str(tmp_path)
    assert facts["connector_state"]["source"] == "weixin"


def test_current_state_filters_loop_runs_by_visible_goal_context(tmp_path):
    visible_goal = GoalStore(tmp_path).create(
        objective="visible goal",
        workspace=str(tmp_path),
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
    )
    hidden_goal = GoalStore(tmp_path).create(
        objective="hidden goal",
        workspace=str(tmp_path),
        source="telegram",
        peer_id="other-peer",
        sender_id="other-sender",
    )
    visible_run = LoopRunStore(tmp_path).create_run(_loop_spec(visible_goal.id, str(tmp_path)))
    LoopRunStore(tmp_path).create_run(_loop_spec(hidden_goal.id, str(tmp_path)))

    facts = current_state_facts(
        CurrentStateBuilder(tmp_path).build(
            SurfaceContext(
                home=tmp_path,
                source="weixin",
                peer_id="peer-1",
                sender_id="sender-1",
                workspace=str(tmp_path),
            )
        )
    )

    assert [item["id"] for item in facts["active_goals"]] == [visible_goal.id]
    assert [item["run_id"] for item in facts["active_loop_runs"]] == [visible_run.run_id]


def test_current_state_includes_active_shadow_workspaces(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("base\n", encoding="utf-8")
    shadow = ShadowWorkspaceManager(tmp_path).create_shadow(
        run_id="loop-run-1",
        workspace=workspace,
    )

    facts = current_state_facts(
        CurrentStateBuilder(tmp_path).build(
            SurfaceContext(
                home=tmp_path,
                source="cli",
                peer_id="cli",
                sender_id="tester",
                workspace=str(workspace),
            )
        )
    )

    assert facts["workspace_state"]["shadow_workspace"] == shadow.shadow_workspace
    assert facts["workspace_state"]["shadow_workspaces"][0]["run_id"] == "loop-run-1"


@pytest.mark.asyncio
async def test_connector_source_and_ingress_facts_survive_shared_planner_boundary(tmp_path):
    provider = _CapturingPlannerProvider()
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    controller = TurnController(
        home=tmp_path,
        runtime=runtime,
        project_dir=tmp_path,
    )
    _, _, context, runtime_facts = controller._initialize_turn(
        "inspect current work",
        "peer-1",
        "sender-1",
        "connector.weixin",
        "session-1",
        None,
        {"connector_message": {"message_id": "message-1"}},
    )
    spec = LoopSpec.from_goal(
        GoalSpec(
            objective="inspect current work",
            scope=(f"repo:{tmp_path}",),
            acceptance_criteria=("respond from current facts",),
            permission_ceiling="write",
            owner="sender-1",
        ),
        goal_id="goal-shared-policy",
        allowed_capabilities=("*",),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.LLM_CHECKER,
                name="objective_check",
                evidence_key="semantic_checker_result",
            ),
        ),
    )
    state = LoopRunStore(tmp_path).create_run(spec)
    capabilities = CapabilityRegistry(
        home=tmp_path,
        project_dir=tmp_path,
        permission_ceiling=context.permission_ceiling,
    )

    planned = await ModelCapabilityPlannerPort(
        runtime=runtime,
        capabilities=capabilities,
        context=context,
    ).plan(spec, state, workspace=tmp_path, evidence={})

    assert planned.tool == "respond"
    turn_input = provider.messages[-1].content
    facts_match = re.search(
        r"<runtime_facts>\s*(.*?)\s*</runtime_facts>",
        turn_input,
        re.DOTALL,
    )
    assert facts_match is not None
    planner_facts = json.loads(facts_match.group(1))
    assert planner_facts["ingress_facts"] == runtime_facts
    assert planner_facts["ingress_facts"]["current_state"]["connector_state"][
        "source"
    ] == "connector.weixin"
    assert planner_facts["ingress_facts"]["intent_facts"]["connector_message"][
        "message_id"
    ] == "message-1"
    manifest = json.loads(turn_input.split("[TOOL MANIFEST]\n", 1)[1])
    manifest_names = {item["name"] for item in manifest}
    assert {"respond", "shell.run", "web.search"} <= manifest_names
