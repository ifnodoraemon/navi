from __future__ import annotations

import json
import re

import pytest

from navi.capabilities import CapabilityRegistry, build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.control import ApprovalService, CurrentStateBuilder, SurfaceContext, current_state_facts
from navi.goals import GoalStore
from navi.lifecycle import Governance, Phase, Resolution
from navi.loop_contracts import (
    GoalSpec,
    LoopNode,
    LoopSpec,
    LoopTerminalState,
    VerificationKind,
    VerificationStep,
)
from navi.loop_runs import LoopRunStore
from navi.provider import ChatMessage
from navi.runtime import AgentRuntime
from navi.runs import RunStore
from navi.state_graph import ModelCapabilityPlannerPort


class _NoModelCalls:
    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        raise AssertionError(
            f"model must not be called while resolving a superseded approval: {role}"
        )

    def list_roles(self) -> list[str]:
        return ["planner", "checker", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class _CapturePlanner:
    def __init__(self) -> None:
        self.messages: list[ChatMessage] = []

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        assert role == "planner"
        self.messages = messages
        return json.dumps(
            {
                "syscalls": [
                    {
                        "tool": "respond",
                        "permission": "read",
                        "args": {"message": "state inspected"},
                        "reason": "respond from refreshed state",
                    }
                ]
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "checker", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class _CorrectedTerminalResponseProvider:
    def __init__(self) -> None:
        self.planner_calls = 0
        self.checker_calls = 0

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        if role == "planner":
            self.planner_calls += 1
            message = (
                "I need to stop here." if self.planner_calls == 1 else "The verified answer is 42."
            )
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "respond",
                            "permission": "read",
                            "args": {"message": message},
                            "reason": "candidate response",
                        }
                    ]
                }
            )
        if role == "checker":
            self.checker_calls += 1
            return json.dumps(
                {
                    "passed": self.checker_calls == 2,
                    "evidence_summary": (
                        "the objective is complete"
                        if self.checker_calls == 2
                        else "the objective is not complete"
                    ),
                }
            )
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "checker", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class _PlannerContractRepairProvider:
    def __init__(self) -> None:
        self.planner_calls = 0
        self.checker_calls = 0
        self.repair_prompt = ""

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        if role == "planner":
            self.planner_calls += 1
            if self.planner_calls == 1:
                return json.dumps(
                    {
                        "syscalls": [
                            {
                                "tool": "respond",
                                "permission": "read",
                                "args": {"message": "draft answer"},
                                "reason": "answer the user",
                            },
                            {
                                "tool": "memory.add",
                                "permission": "write",
                                "args": {
                                    "operation": "add",
                                    "type": "note",
                                    "content": "remember the draft",
                                    "scope": "session",
                                    "reason": "store a session note",
                                    "provenance": "test",
                                },
                                "reason": "save related memory",
                            },
                        ]
                    }
                )
            self.repair_prompt = messages[-1].content
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "respond",
                            "permission": "read",
                            "args": {
                                "message": "委派子 agent 时，应由模型基于任务复杂度、并行性和持续性自行决定。"
                            },
                            "reason": "repair previous multi-syscall planner output",
                        }
                    ]
                }
            )
        if role == "checker":
            self.checker_calls += 1
            return json.dumps(
                {
                    "passed": True,
                    "evidence_summary": "the response answers the delegation question",
                }
            )
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "checker", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class _RepeatedFactProvider:
    def __init__(self) -> None:
        self.planner_calls = 0

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        if role == "planner":
            self.planner_calls += 1
            return json.dumps(
                {
                    "syscalls": [
                        {
                            "tool": "shell.run",
                            "permission": "read",
                            "args": {"command": ["ls", "-1"], "cwd": "."},
                            "reason": "inspect the same directory",
                        }
                    ]
                }
            )
        if role == "checker":
            return json.dumps(
                {
                    "passed": False,
                    "should_continue": True,
                    "evidence_summary": "no new completion evidence",
                }
            )
        raise AssertionError(f"unexpected role: {role}")

    def list_roles(self) -> list[str]:
        return ["planner", "checker", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class _PlannerFailureProvider:
    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        raise RuntimeError("provider unavailable")

    def list_roles(self) -> list[str]:
        return ["planner", "checker", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


def _surface(home) -> SurfaceContext:
    return SurfaceContext(
        home=home,
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        session_id="session-1",
        workspace=str(home),
        input_text="确认",
    )


def _semantic_spec(goal_id: str, objective: str, workspace: str) -> LoopSpec:
    return LoopSpec.from_goal(
        GoalSpec(
            objective=objective,
            scope=(f"repo:{workspace}",),
            acceptance_criteria=("objective evidence is accepted",),
            permission_ceiling="write",
            owner="sender-1",
            metadata={"session_id": "session-1"},
        ),
        goal_id=goal_id,
        allowed_capabilities=("respond", "shell.run"),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.LLM_CHECKER,
                name="objective_check",
                evidence_key="semantic_checker_result",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_superseded_approved_code_does_not_resume_current_approval_gate(tmp_path) -> None:
    runs = RunStore(tmp_path)
    run = runs.create(
        "send the corrected file",
        kind="loop:turn",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase=Phase.PAUSED,
        governance=Governance.AWAITING_APPROVAL,
        resolution=Resolution.BLOCKED,
    )
    goal = GoalStore(tmp_path).create(
        objective="send the corrected file",
        workspace=str(tmp_path),
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        session_id="session-1",
        run_id=run.id,
    )
    old = runs.create_approval(
        run_id=run.id,
        action="capability",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        requested_tool="channel.send_file",
        requested_permission="write",
        args_json='{"path":"resume.md"}',
        code="381894",
    )
    runs.resolve_approval(old.id, decision="approve", resolved_by="sender-1")
    current = runs.create_approval(
        run_id=run.id,
        action="capability",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        requested_tool="shell.run",
        requested_permission="write",
        args_json='{"command":["find","."]}',
        code="580511",
    )
    spec = _semantic_spec(goal.id, goal.objective, str(tmp_path))
    loop_run = LoopRunStore(tmp_path).create_run(
        spec,
        node=LoopNode.ESCALATE,
        terminal_state=LoopTerminalState.WAITING_APPROVAL,
        evidence={
            "action": "approval",
            "facts": {"approval": {"id": current.id, "code": current.code}},
        },
    )

    result = await ApprovalService(tmp_path).resolve_and_continue(
        decision="approve",
        selection="explicit_code",
        context=_surface(tmp_path),
        runtime=AgentRuntime(home=tmp_path, provider=_NoModelCalls()),
        code=old.code,
    )

    assert result.ok is True
    assert result.facts["state_transition"] == "already_approved"
    assert result.facts["continuation_status"] == "waiting_approval"
    assert result.facts["current_approval"]["id"] == current.id
    assert LoopRunStore(tmp_path).get_run(loop_run.run_id).terminal_state == (
        LoopTerminalState.WAITING_APPROVAL
    )
    assert runs.get_approval(current.id).status == "pending"

    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=_NoModelCalls()),
    )
    control_result = await registry.invoke(
        "approval.resolve",
        {"decision": "approve", "code": old.code},
        permission="prepare",
        context=CapabilityContext(
            home=tmp_path,
            source="weixin",
            peer_id="peer-1",
            sender_id="sender-1",
            session_id="session-1",
            workspace=str(tmp_path),
            input_text=f"批准 {old.code}",
        ),
    )
    assert control_result.ok is True
    assert control_result.facts["continuation_status"] == "waiting_approval"
    assert control_result.yields_control is False


@pytest.mark.asyncio
async def test_planner_rebuilds_current_state_after_approval_changes(tmp_path) -> None:
    runs = RunStore(tmp_path)
    run = runs.create(
        "approval state",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase=Phase.PAUSED,
        governance=Governance.AWAITING_APPROVAL,
        resolution=Resolution.BLOCKED,
    )
    old = runs.create_approval(
        run_id=run.id,
        action="capability",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        requested_tool="channel.send_file",
        requested_permission="write",
        code="381894",
    )
    stale_state = current_state_facts(CurrentStateBuilder(tmp_path).build(_surface(tmp_path)))
    runs.resolve_approval(old.id, decision="approve", resolved_by="sender-1")
    current = runs.create_approval(
        run_id=run.id,
        action="capability",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        requested_tool="shell.run",
        requested_permission="write",
        code="580511",
    )
    provider = _CapturePlanner()
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    context = CapabilityContext(
        home=tmp_path,
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        session_id="session-1",
        workspace=str(tmp_path),
        runtime_facts={"current_state": stale_state},
    )
    spec = _semantic_spec("goal-state-refresh", "inspect approval", str(tmp_path))
    state = LoopRunStore(tmp_path).create_run(spec)

    await ModelCapabilityPlannerPort(
        runtime=runtime,
        capabilities=CapabilityRegistry(home=tmp_path, project_dir=tmp_path),
        context=context,
    ).plan(spec, state, workspace=tmp_path, evidence={})

    turn_input = provider.messages[-1].content
    match = re.search(r"<runtime_facts>\s*(.*?)\s*</runtime_facts>", turn_input, re.DOTALL)
    assert match is not None
    planner_facts = json.loads(match.group(1))
    pending = planner_facts["ingress_facts"]["current_state"]["pending_approvals"]
    assert [item["id"] for item in pending] == [current.id]
    assert all(item["id"] != old.id for item in pending)


@pytest.mark.asyncio
async def test_checker_rejected_response_is_replanned_before_delivery(tmp_path) -> None:
    provider = _CorrectedTerminalResponseProvider()
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
    )

    result = await registry.invoke(
        "goal.open",
        {
            "objective": "answer once and stop",
            "workspace": str(tmp_path),
            "loop_kind": "turn",
            "allowed_capabilities": ["respond"],
        },
        permission="prepare",
        context=CapabilityContext(home=tmp_path, workspace=str(tmp_path)),
    )

    assert provider.planner_calls == 2
    assert provider.checker_calls == 2
    assert result.ok is True
    assert result.facts["loop_terminal_state"] == LoopTerminalState.CONVERGED
    assert result.facts["responded_message"] == "The verified answer is 42."
    assert "I need to stop here." not in result.facts["responded_message"]


@pytest.mark.asyncio
async def test_planner_contract_error_is_reflected_back_to_model(tmp_path) -> None:
    provider = _PlannerContractRepairProvider()
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(tmp_path, project_dir=tmp_path, runtime=runtime)

    result = await registry.invoke(
        "goal.open",
        {
            "objective": "我们什么情况下会委派子agent处理任务",
            "workspace": str(tmp_path),
            "loop_kind": "turn",
            "allowed_capabilities": ["respond", "memory.add"],
        },
        permission="prepare",
        context=CapabilityContext(home=tmp_path, workspace=str(tmp_path), session_id="session-1"),
    )

    assert provider.planner_calls == 2
    assert provider.checker_calls == 1
    assert result.ok is True
    assert result.facts["loop_terminal_state"] == LoopTerminalState.CONVERGED
    assert result.facts["responded_message"].startswith("委派子 agent 时")
    assert "planner_must_return_exactly_one_syscall" in provider.repair_prompt
    assert "candidate_syscalls" in provider.repair_prompt
    evidence = result.facts["state_graph_result"]["evidence"]
    first_failure = evidence["planner_failure_history"][0]["planned_capability"]
    assert first_failure["args"]["candidate_syscalls"][0]["tool"] == "respond"
    assert first_failure["args"]["candidate_syscalls"][1]["tool"] == "memory.add"


@pytest.mark.asyncio
async def test_repeated_progress_is_bounded_before_max_attempts(tmp_path) -> None:
    provider = _RepeatedFactProvider()
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=AgentRuntime(home=tmp_path, provider=provider),
    )

    result = await registry.invoke(
        "goal.open",
        {
            "objective": "find evidence that does not exist",
            "workspace": str(tmp_path),
            "loop_kind": "turn",
            "allowed_capabilities": ["shell.run"],
        },
        permission="prepare",
        context=CapabilityContext(home=tmp_path, workspace=str(tmp_path)),
    )

    assert result.ok is False
    assert result.facts["loop_terminal_state"] == LoopTerminalState.BLOCKED
    assert provider.planner_calls == 4
    evidence = result.facts["state_graph_result"]["evidence"]
    assert evidence["loop_gate"]["reason"] == "repeated_progress_signature"
    assert evidence["loop_gate"]["warning_count"] == 2
    assert evidence["progress_signature"]


@pytest.mark.asyncio
async def test_goal_policy_envelope_cannot_be_widened_by_model_arguments(tmp_path) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)

    result = await registry.invoke(
        "goal.open",
        {
            "objective": "escape the turn policy",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["shell.run"],
            "auto_start": False,
        },
        permission="prepare",
        context=CapabilityContext(
            home=tmp_path,
            workspace=str(tmp_path),
            allowed_tools=frozenset({"respond"}),
        ),
    )

    assert result.ok is False
    assert result.error_reason == "schema_mismatch"
    assert "outside the current policy envelope" in result.message


@pytest.mark.asyncio
async def test_provider_exception_closes_goal_run_and_loop_lifecycle(tmp_path) -> None:
    runtime = AgentRuntime(home=tmp_path, provider=_PlannerFailureProvider())
    registry = build_capability_registry(tmp_path, project_dir=tmp_path, runtime=runtime)

    result = await registry.invoke(
        "goal.open",
        {
            "objective": "exercise provider failure",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["respond"],
        },
        permission="prepare",
        context=CapabilityContext(home=tmp_path, workspace=str(tmp_path)),
    )

    assert result.ok is False
    goal = GoalStore(tmp_path).list(limit=1)[0]
    run = RunStore(tmp_path).get(goal.run_id)
    loop_run = LoopRunStore(tmp_path).list_by_goal(goal.id, limit=1)[0]
    assert run is not None
    assert run.phase == Phase.ENDED
    assert run.resolution == Resolution.FAILED
    assert goal.phase == Phase.ENDED
    assert goal.resolution == Resolution.FAILED
    assert loop_run.terminal_state == LoopTerminalState.FAILED


def test_delivery_receipt_is_the_authoritative_completion_boundary(tmp_path) -> None:
    runs = RunStore(tmp_path)
    run = runs.create(
        "deliver report",
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        workspace=str(tmp_path),
        phase=Phase.PAUSED,
        resolution=Resolution.BLOCKED,
    )
    goal = GoalStore(tmp_path).create(
        objective="deliver report",
        workspace=str(tmp_path),
        source="weixin",
        peer_id="peer-1",
        sender_id="sender-1",
        run_id=run.id,
    )
    spec = _semantic_spec(goal.id, goal.objective, str(tmp_path))
    loop_run = LoopRunStore(tmp_path).create_run(
        spec,
        node=LoopNode.PAUSE,
        terminal_state=LoopTerminalState.PAUSED,
        evidence={"action": "connector_outbound"},
    )

    GoalStore(tmp_path).record_delivery(
        run_id=run.id,
        channel="weixin",
        text_preview="report delivered",
        text_length=16,
        media_count=1,
        delivery_id=loop_run.run_id,
    )

    completed_run = runs.get(run.id)
    completed_goal = GoalStore(tmp_path).get(goal.id)
    completed_loop = LoopRunStore(tmp_path).get_run(loop_run.run_id)
    assert completed_run is not None and completed_run.resolution == Resolution.SUCCESS
    assert completed_goal is not None and completed_goal.resolution == Resolution.SUCCESS
    assert completed_loop is not None
    assert completed_loop.terminal_state == LoopTerminalState.CONVERGED
