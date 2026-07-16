from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from navi.capabilities import CapabilityRegistry
from navi.capabilities_types import CapabilityResult
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
from navi.runs import RunStore
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
                        "reason": "ask from current facts",
                    }
                ]
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "executor", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class _FactResponderProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def complete_for(
        self,
        role: str,
        messages: list[ChatMessage],
        **kwargs,
    ) -> str:
        self.calls.append((role, messages))
        assert role == "responder"
        return "模型根据失败事实生成的说明。"

    def list_roles(self) -> list[str]:
        return ["planner", "responder"]

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
        allowed_capabilities=("filesystem.write", "shell.run"),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.COMMAND_EXIT_CODE,
                name="tests",
                command="pytest -q",
                timeout=TimeoutPolicy(seconds=120),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_turn_controller_never_surfaces_capability_observation_as_user_copy(
    tmp_path,
    monkeypatch,
):
    provider = _FactResponderProvider()
    controller = TurnController(
        home=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
        project_dir=tmp_path,
    )

    async def invoke(*args, **kwargs):
        return CapabilityResult(
            ok=False,
            action="error",
            message="machine-only failure observation",
            error_reason="internal_error",
            facts={"error_type": "RuntimeError"},
        )

    monkeypatch.setattr(controller.capabilities, "invoke", invoke)

    result = await controller.handle(
        "完成这个请求",
        peer_id="cli",
        sender_id="tester",
        source="cli",
    )

    assert result.text == "模型根据失败事实生成的说明。"
    assert result.text != "machine-only failure observation"
    assert [role for role, _ in provider.calls] == ["responder"]
    assert "machine-only failure observation" in provider.calls[0][1][-1].content


@pytest.mark.asyncio
async def test_turn_controller_preallocates_session_for_first_turn(
    tmp_path,
    monkeypatch,
):
    provider = _FactResponderProvider()
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    controller = TurnController(
        home=tmp_path,
        runtime=runtime,
        project_dir=tmp_path,
    )
    captured: dict[str, str] = {}

    async def invoke(name, args, *, permission, context):
        captured["name"] = name
        captured["session_id"] = context.session_id or ""
        captured["trace_id"] = context.trace_id
        return CapabilityResult(
            ok=True,
            action="respond",
            facts={"responded_message": "ok"},
            terminal=True,
            run_id="run-1",
        )

    monkeypatch.setattr(controller.capabilities, "invoke", invoke)

    result = await controller.handle(
        "hello",
        peer_id="cli-peer",
        sender_id="cli-sender",
        source="cli",
    )
    messages = runtime.memory.get_messages(result.session_id)

    assert captured["name"] == "goal.open"
    assert captured["session_id"]
    assert captured["session_id"] == result.session_id
    assert messages[0].source == "cli"
    assert messages[0].peer_id == "cli-peer"
    assert messages[0].sender_id == "cli-sender"
    assert messages[0].trace_id == captured["trace_id"]


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


def test_current_state_actor_scope_precedes_global_noise_limits(tmp_path):
    runs = RunStore(tmp_path)
    visible_run = runs.create(
        "visible run",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase="running",
    )
    visible_approval = runs.create_approval(
        run_id=visible_run.id,
        action="run_execution",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
    )
    visible_goal = GoalStore(tmp_path).create(
        objective="visible older goal",
        workspace=str(tmp_path),
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        run_id=visible_run.id,
    )
    visible_loop = LoopRunStore(tmp_path).create_run(_loop_spec(visible_goal.id, str(tmp_path)))

    for index in range(101):
        hidden_run = runs.create(
            f"hidden run {index}",
            source="telegram",
            peer_id="other-peer",
            sender_id="other-sender",
            workspace=str(tmp_path),
            phase="running",
        )
        runs.create_approval(
            run_id=hidden_run.id,
            action="run_execution",
            source="telegram",
            peer_id="other-peer",
            sender_id="other-sender",
        )
        GoalStore(tmp_path).create(
            objective=f"hidden newer goal {index}",
            workspace=str(tmp_path),
            source="telegram",
            peer_id="other-peer",
            sender_id="other-sender",
            run_id=hidden_run.id,
        )

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

    assert [item["id"] for item in facts["active_runs"]] == [visible_run.id]
    assert [item["id"] for item in facts["pending_approvals"]] == [visible_approval.id]
    assert [item["id"] for item in facts["active_goals"]] == [visible_goal.id]
    assert [item["run_id"] for item in facts["active_loop_runs"]] == [visible_loop.run_id]
    assert [item["goal_id"] for item in facts["recent_goal_outcomes"]] == [visible_goal.id]


def test_current_state_separates_recent_outcomes_from_orphan_runtime_state(tmp_path):
    runs = RunStore(tmp_path)
    visible_run = runs.create(
        "visible recent result",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase="ended",
    )
    runs.update_run(
        visible_run.id,
        result_summary="model-authored candidate summary\napproval_code=must-not-leak",
        resolution="success",
    )
    visible_goal = GoalStore(tmp_path).create(
        objective="return the latest boot time",
        workspace=str(tmp_path),
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        run_id=visible_run.id,
    )
    hidden_run = runs.create(
        "hidden recent result",
        source="telegram",
        peer_id="other-peer",
        sender_id="other-sender",
        workspace=str(tmp_path),
        phase="ended",
    )
    GoalStore(tmp_path).create(
        objective="hidden task",
        workspace=str(tmp_path),
        source="telegram",
        peer_id="other-peer",
        sender_id="other-sender",
        run_id=hidden_run.id,
    )
    orphan = runs.create(
        "stale active approval envelope",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase="running",
    )

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

    assert [item["goal_id"] for item in facts["recent_goal_outcomes"]] == [visible_goal.id]
    recent = facts["recent_goal_outcomes"][0]
    assert recent["objective"] == "return the latest boot time"
    assert recent["result_summary"] == "model-authored candidate summary"
    assert recent["result_summary_provenance"] == ("assistant_candidate_non_authoritative")
    anomalies = facts["runtime_state_anomalies"]
    assert anomalies["active_run_without_active_goal_count"] == 1
    assert anomalies["active_runs_without_active_goals"][0]["run_id"] == orphan.id


def test_current_state_includes_only_context_matching_delivery_facts(tmp_path):
    visible = GoalStore(tmp_path).create(
        objective="visible delivery",
        workspace=str(tmp_path),
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        run_id="run-visible",
    )
    hidden = GoalStore(tmp_path).create(
        objective="hidden delivery",
        workspace=str(tmp_path),
        source="telegram",
        peer_id="peer-2",
        sender_id="sender-2",
        run_id="run-hidden",
    )
    store = GoalStore(tmp_path)
    store.record_delivery(
        run_id=visible.run_id,
        channel="weixin",
        text_preview="sent lesson",
        text_length=11,
        media_count=0,
    )
    store.record_delivery(
        run_id=visible.run_id,
        channel="weixin",
        text_preview="corrected receipt",
        text_length=17,
        media_count=0,
        sent_at=123.0,
    )
    store.record_delivery(
        run_id=hidden.run_id,
        channel="telegram",
        text_preview="hidden",
        text_length=6,
        media_count=0,
    )

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

    assert len(facts["recent_deliveries"]) == 1
    assert facts["recent_deliveries"][0]["run_id"] == visible.run_id
    assert facts["recent_deliveries"][0]["channel"] == "weixin"
    assert facts["recent_deliveries"][0]["sent_at"] == 123.0


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
        {
            "connector_message": {"message_id": "message-1"},
            "current_state": {"stale": "x" * 20_000},
        },
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
    state = replace(
        LoopRunStore(tmp_path).create_run(spec),
        evidence={"durable_payload": "x" * 20_000},
    )
    capabilities = CapabilityRegistry(
        home=tmp_path,
        project_dir=tmp_path,
        permission_ceiling=context.permission_ceiling,
    )

    planned = await ModelCapabilityPlannerPort(
        runtime=runtime,
        capabilities=capabilities,
        context=context,
    ).plan(
        spec,
        state,
        workspace=tmp_path,
        evidence={
            "capability_result": {"facts": {"latest": "kept"}},
            "attempt_history": [
                {
                    "attempt": 1,
                    "tool": "web.search",
                    "args": {"query": "recent jobs"},
                    "ok": True,
                    "facts": {"results": ["x" * 20_000]},
                    "message": "x" * 20_000,
                    "progress_signature": "sig-1",
                }
            ],
        },
    )

    assert planned.tool == "respond"
    turn_input = provider.messages[-1].content
    facts_match = re.search(
        r"<runtime_facts>\s*(.*?)\s*</runtime_facts>",
        turn_input,
        re.DOTALL,
    )
    assert facts_match is not None
    planner_facts = json.loads(facts_match.group(1))
    assert "current_state" not in planner_facts["ingress_facts"]["intent_facts"]
    assert "evidence" not in planner_facts["loop_run_state"]
    assert planner_facts["loop_run_state"]["evidence_keys"] == ["durable_payload"]
    assert "attempt_history" not in planner_facts["objective_evidence"]
    assert planner_facts["objective_evidence"]["capability_result"]["facts"] == {"latest": "kept"}
    assert planner_facts["attempt_history"] == [
        {
            "args": {"query": "recent jobs"},
            "attempt": 1,
            "fact_keys": ["results"],
            "message_present": True,
            "ok": True,
            "progress_signature": "sig-1",
            "tool": "web.search",
        }
    ]
    assert (
        planner_facts["ingress_facts"]["current_state"]["current_time"]["unix"]
        >= (runtime_facts["current_state"]["current_time"]["unix"])
    )
    assert (
        planner_facts["ingress_facts"]["current_state"]["connector_state"]["source"]
        == "connector.weixin"
    )
    assert (
        planner_facts["ingress_facts"]["intent_facts"]["connector_message"]["message_id"]
        == "message-1"
    )
    assert "[MODEL ROLES]" not in turn_input
    assert "[MODEL ROLE CONTRACTS]" not in turn_input
    manifest = json.loads(turn_input.split("[TOOL MANIFEST]\n", 1)[1])
    manifest_names = {item["name"] for item in manifest}
    assert {"respond", "shell.run", "web.search"} <= manifest_names


@pytest.mark.asyncio
async def test_planner_ingress_projects_ambient_goal_outcomes_by_task_context(
    tmp_path,
):
    provider = _CapturingPlannerProvider()
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    controller = TurnController(
        home=tmp_path,
        runtime=runtime,
        project_dir=tmp_path,
    )
    runs = RunStore(tmp_path)
    goals = GoalStore(tmp_path)

    leak_text = "AI Knowledge Lesson 5 should not leak into this task"
    active_leak_text = "AI active task title should not leak into this task"
    runs.create(
        active_leak_text,
        source="connector.weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase="running",
    )
    ambient_run = runs.create(
        "ambient task result",
        source="connector.weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase="ended",
    )
    goals.create(
        objective="ambient progressive task",
        workspace=str(tmp_path),
        source="connector.weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        session_id="session-1",
        run_id=ambient_run.id,
        parent_goal_id="ambient-lineage",
    )
    updated_ambient = runs.update_run(
        ambient_run.id,
        phase="ended",
        acceptance="accepted",
        resolution="success",
        result_summary=leak_text,
    )
    assert updated_ambient is not None
    goals.update_for_run(updated_ambient)
    goals.record_delivery(
        run_id=ambient_run.id,
        channel="weixin",
        text_preview=leak_text,
        text_length=len(leak_text),
        media_count=0,
    )

    current_lineage = "current-lineage"
    allowed_text = "General knowledge Lesson 1 is authoritative here"
    current_prior_run = runs.create(
        "current lineage prior result",
        source="connector.weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase="ended",
    )
    current_prior_goal = goals.create(
        objective="current progressive task",
        workspace=str(tmp_path),
        source="connector.weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        session_id="session-1",
        run_id=current_prior_run.id,
        parent_goal_id=current_lineage,
    )
    updated_current = runs.update_run(
        current_prior_run.id,
        phase="ended",
        acceptance="accepted",
        resolution="success",
        result_summary=allowed_text,
    )
    assert updated_current is not None
    goals.update_for_run(updated_current)

    _, _, context, _ = controller._initialize_turn(
        "continue the current progressive task",
        "peer-1",
        "sender-1",
        "connector.weixin",
        "session-1",
        None,
        {},
    )
    task_context = {
        "lineage": {
            "id": current_lineage,
            "kind": "recurring_goal",
        },
        "progress": {
            "scope": "lineage",
            "sequence_number": 2,
            "authority": "same_lineage_authoritative_prior_items",
            "authoritative_prior_items": [
                {
                    "goal_id": current_prior_goal.id,
                    "run_id": current_prior_run.id,
                    "result_summary": allowed_text,
                }
            ],
            "ambient_history_authoritative": False,
        },
    }
    spec = LoopSpec.from_goal(
        GoalSpec(
            objective="continue the current progressive task",
            scope=(f"repo:{tmp_path}",),
            acceptance_criteria=("respond from current task context",),
            permission_ceiling="write",
            owner="sender-1",
            metadata={
                "source": "connector.weixin",
                "peer_id": "peer-1",
                "sender_id": "sender-1",
                "session_id": "session-1",
                "workspace": str(tmp_path),
                "task_context": task_context,
            },
        ),
        goal_id="current-occurrence",
        allowed_capabilities=("respond",),
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
    ).plan(
        spec,
        state,
        workspace=tmp_path,
        evidence={},
    )

    assert planned.tool == "respond"
    turn_input = provider.messages[-1].content
    assert leak_text not in turn_input
    assert active_leak_text not in turn_input
    facts_match = re.search(
        r"<runtime_facts>\s*(.*?)\s*</runtime_facts>",
        turn_input,
        re.DOTALL,
    )
    assert facts_match is not None
    planner_facts = json.loads(facts_match.group(1))
    ingress_facts = planner_facts["ingress_facts"]
    assert ingress_facts["task_context"]["lineage"]["id"] == current_lineage
    assert ingress_facts["task_context"]["progress"]["sequence_number"] == 2
    current_state = ingress_facts["current_state"]
    assert [item["goal_id"] for item in current_state["recent_goal_outcomes"]] == [
        current_prior_goal.id
    ]
    assert current_state["recent_goal_outcomes"][0]["result_summary"] == allowed_text
    assert current_state["ambient_goal_outcomes"][0]["goal_id"]
    assert current_state["ambient_goal_outcomes"][0]["result_summary_omitted"] is True
    assert current_state["ambient_goal_outcomes"][0]["objective_omitted"] is True
    assert current_state["ambient_active_runs"][0]["title_omitted"] is True
    assert current_state["ambient_recent_deliveries"][0]["text_preview_omitted"] is True
    assert current_state["task_projection_policy"]["ambient_goal_outcome_count"] == 1
    assert current_state["task_projection_policy"]["ambient_active_run_count"] == 1
