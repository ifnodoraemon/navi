from __future__ import annotations

import json
import re
import shlex
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from navi.capabilities import CapabilityRegistry
from navi.capabilities_types import CapabilityContext
from navi.loop_contracts import (
    BudgetPolicy,
    GoalSpec,
    LoopNode,
    LoopRunState,
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
    _transition_loop_decision,
)
from navi.trace import TraceStore


def _command(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def _loop_decision_payloads_by_tool(home: Path, trace_id: str, tool: str) -> list[dict]:
    return [
        json.loads(event.output_json)
        for event in TraceStore(home).list_loop_decisions(trace_id)
        if event.tool == tool and event.output_json.strip()
    ]


def _planner_turn_input(provider: object) -> str:
    calls = getattr(provider, "calls")
    assert calls
    messages = calls[0][1]
    return messages[-1].content


def _runtime_facts_from_turn_input(turn_input: str) -> dict:
    match = re.search(r"<runtime_facts>\s*(.*?)\s*</runtime_facts>", turn_input, re.DOTALL)
    assert match is not None
    return json.loads(match.group(1))


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


def _send_file_spec(command: str, *, timeout: float = 5.0) -> LoopSpec:
    return LoopSpec.from_goal(
        GoalSpec(
            objective="send the requested file through the connector",
            scope=("repo:/tmp/project",),
            acceptance_criteria=("outbound media is staged and verification passes",),
            permission_ceiling="write",
            owner="tester",
        ),
        goal_id="goal-1",
        allowed_capabilities=("channel.send_file",),
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


class _MemoryAwarePlanningProvider(_PlanningProvider):
    def __init__(self, used_memory_ids: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.used_memory_ids = used_memory_ids

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        self.calls.append((role, messages))
        assert role == "planner"
        syscall = {
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
        if self.used_memory_ids:
            syscall["used_memory_ids"] = list(self.used_memory_ids)
        return json.dumps({"syscalls": [syscall]})


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


class _SendFilePlanningProvider:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        del kwargs
        self.calls.append((role, messages))
        assert role == "planner"
        return json.dumps(
            {
                "syscalls": [
                    {
                        "tool": "channel.send_file",
                        "permission": "write",
                        "args": {"path": str(self.path)},
                        "model_role": "executor",
                        "reason": "stage outbound media for connector delivery",
                    }
                ]
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "executor", "checker", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


def test_durable_state_graph_sync_runner_is_disabled(tmp_path):
    runner = DurableStateGraphRunner(home=tmp_path)

    with pytest.raises(RuntimeError, match="run\\(\\) is disabled"):
        runner.run(_spec(_command("print('ok')")), workspace=tmp_path)


def test_transition_decision_promotes_explicit_side_effect_summary() -> None:
    decision = _transition_loop_decision(
        from_state=LoopRunState(
            run_id="loop-1",
            goal_id="goal-1",
            loop_spec_id="spec-1",
            node=LoopNode.EXECUTE,
        ),
        to_state=LoopRunState(
            run_id="loop-1",
            goal_id="goal-1",
            loop_spec_id="spec-1",
            node=LoopNode.EVALUATE,
        ),
        checkpoint_id="checkpoint-1",
        condition="side_effect_recorded",
        terminal_state="",
        evidence={
            "executor": {
                "action": "connector_outbound",
                "facts": {
                    "side_effect_scope": "external",
                    "side_effect_state": "staged",
                    "outbound_path": "/tmp/outbox/report.pdf",
                    "side_effect_commit": "weixin.connector_runtime.dispatch_outbox",
                    "side_effect_compensate": "filesystem.remove_staged_outbound",
                },
            }
        },
    )

    assert decision.evidence["side_effect"] == {
        "scope": "external",
        "state": "staged",
        "artifact": "/tmp/outbox/report.pdf",
        "action": "connector_outbound",
        "commit": "weixin.connector_runtime.dispatch_outbox",
        "compensate": "filesystem.remove_staged_outbound",
    }


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
async def test_planner_context_compacts_long_session_history(tmp_path: Path) -> None:
    provider = _PlanningProvider()
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    session_id = runtime.memory.create_session()
    for index in range(18):
        role = "user" if index % 2 == 0 else "assistant"
        if index == 1:
            content = "legacy-start " + ("x" * 800) + " legacy-tail-marker"
        elif index == 17:
            content = "recent full marker survives compaction"
        else:
            content = f"turn-{index} context"
        runtime.memory.add_message(session_id, role, content)
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
    spec = _write_spec(_command("from pathlib import Path; assert Path('app.py').exists()"))
    spec = replace(
        spec,
        goal=replace(
            spec.goal,
            metadata={**spec.goal.metadata, "session_id": session_id},
        ),
    )

    result = await runner.run_async(spec, workspace=tmp_path)

    assert result.terminal_state == LoopTerminalState.CONVERGED
    turn_input = _planner_turn_input(provider)
    conversation = re.search(
        r"<conversation_history>\s*(.*?)\s*</conversation_history>",
        turn_input,
        re.DOTALL,
    )
    assert conversation is not None
    conversation_text = conversation.group(1)
    assert "Conversation context was compacted before planner intake." in conversation_text
    assert "legacy-start" in conversation_text
    assert "legacy-tail-marker" not in conversation_text
    assert "recent full marker survives compaction" in conversation_text
    compaction = _runtime_facts_from_turn_input(turn_input)["conversation_compaction"]
    assert compaction["compacted"] is True
    assert compaction["message_count"] == 18
    assert compaction["omitted_message_count"] == 6
    assert compaction["retained_recent_message_count"] == 12
    assert compaction["compacted_character_count"] <= compaction["max_character_count"]


@pytest.mark.asyncio
async def test_planner_records_declared_memory_activation(tmp_path: Path) -> None:
    provider = _MemoryAwarePlanningProvider()
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    item = runtime.memory.add_item(
        "preference",
        "write app.py with the planned content",
        source="test",
        status="active",
        confidence=0.8,
        reason="unit test",
        provenance="tests/test_state_graph.py",
    )
    provider.used_memory_ids = (item.id,)
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
        _write_spec(_command("from pathlib import Path; assert Path('app.py').exists()")),
        workspace=tmp_path,
    )

    turn_input = _planner_turn_input(provider)
    memory_facts = _runtime_facts_from_turn_input(turn_input)["memory_context"]
    planned = result.evidence["planned_capability"]
    updated = runtime.memory.get_item(item.id)
    assert result.terminal_state == LoopTerminalState.CONVERGED
    assert "[MEMORY RECALL]" in turn_input
    assert item.id in turn_input
    assert item.id in memory_facts["candidate_ids"]
    assert planned["used_memory_ids"] == [item.id]
    assert planned["memory_activation"]["activated_ids"] == [item.id]
    assert updated is not None
    assert updated.metadata["recall_count"] == 1
    assert updated.metadata["activation_reason"] == (
        "planner selected file.write using recalled memory"
    )
    assert updated.metadata["activation_provenance"].endswith(":planner")


@pytest.mark.asyncio
async def test_planner_memory_injection_does_not_record_activation_without_model_use(
    tmp_path: Path,
) -> None:
    provider = _MemoryAwarePlanningProvider()
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    item = runtime.memory.add_item(
        "preference",
        "write app.py with the planned content",
        source="test",
        status="active",
        confidence=0.8,
        reason="unit test",
        provenance="tests/test_state_graph.py",
    )
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
        _write_spec(_command("from pathlib import Path; assert Path('app.py').exists()")),
        workspace=tmp_path,
    )

    turn_input = _planner_turn_input(provider)
    memory_facts = _runtime_facts_from_turn_input(turn_input)["memory_context"]
    planned = result.evidence["planned_capability"]
    updated = runtime.memory.get_item(item.id)
    assert result.terminal_state == LoopTerminalState.CONVERGED
    assert item.id in turn_input
    assert item.id in memory_facts["candidate_ids"]
    assert planned["used_memory_ids"] == []
    assert planned["memory_activation"]["activated_count"] == 0
    assert updated is not None
    assert "recall_count" not in updated.metadata


@pytest.mark.asyncio
async def test_durable_state_graph_uses_loop_budget_policy_and_traces_gate(tmp_path):
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
        trace_id="trace-budget-policy",
    )
    runner = DurableStateGraphRunner(
        home=tmp_path,
        planner_port=ModelCapabilityPlannerPort(
            runtime=runtime,
            capabilities=planner_capabilities,
        ),
        executor_port=CapabilityExecutorPort(home=tmp_path, context=context),
        trace_store=TraceStore(tmp_path),
        trace_context=context,
    )
    spec = replace(
        _write_spec(_command("from pathlib import Path; assert Path('app.py').exists()")),
        budget_policy=BudgetPolicy(call_budget=1),
    )

    result = await runner.run_async(spec, workspace=tmp_path)

    assert result.terminal_state == LoopTerminalState.WAITING_APPROVAL
    assert provider.calls and provider.calls[0][0] == "planner"
    assert not (tmp_path / "app.py").exists()
    gate_decisions = [
        json.loads(event.output_json)
        for event in TraceStore(tmp_path).list_loop_decisions("trace-budget-policy")
        if json.loads(event.output_json).get("tool") == "state_graph.execute"
    ]
    assert gate_decisions
    exhausted = gate_decisions[0]["evidence"]["grant"]
    assert exhausted["reason"] == "call_budget_exhausted"
    assert exhausted["budget_state"]["call_budget_remaining"] == 0


@pytest.mark.asyncio
async def test_durable_state_graph_releases_staged_external_side_effect_after_acceptance(
    tmp_path: Path,
) -> None:
    source = tmp_path / "resume.docx"
    source.write_bytes(b"resume")
    provider = _SendFilePlanningProvider(source)
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    context = CapabilityContext(
        home=tmp_path,
        source="state_graph",
        peer_id="wx-user",
        sender_id="tester",
        permission_ceiling="write",
        workspace=str(tmp_path),
        trace_id="trace-side-effect-commit",
    )
    runner = DurableStateGraphRunner(
        home=tmp_path,
        planner_port=ModelCapabilityPlannerPort(
            runtime=runtime,
            capabilities=CapabilityRegistry(
                home=tmp_path,
                project_dir=tmp_path,
                permission_ceiling="write",
            ),
        ),
        executor_port=CapabilityExecutorPort(home=tmp_path, context=context),
        trace_store=TraceStore(tmp_path),
        trace_context=context,
    )

    result = await runner.run_async(
        _send_file_spec(_command("print('ok')")),
        workspace=tmp_path,
    )

    assert result.terminal_state == LoopTerminalState.CONVERGED
    assert result.evidence["capability_result"]["terminal"] is True
    assert result.evidence["side_effect_commit_result"]["state"] == "released_for_connector_commit"
    staged = Path(result.evidence["capability_result"]["facts"]["outbound_path"])
    assert staged.exists()
    commit_decisions = _loop_decision_payloads_by_tool(
        tmp_path,
        "trace-side-effect-commit",
        "state_graph.side_effect.commit",
    )
    assert commit_decisions
    assert (
        commit_decisions[0]["evidence"]["side_effect"]["state"]
        == "released_for_connector_commit"
    )


@pytest.mark.asyncio
async def test_durable_state_graph_compensates_staged_external_side_effect_on_rejection(
    tmp_path: Path,
) -> None:
    source = tmp_path / "resume.docx"
    source.write_bytes(b"resume")
    provider = _SendFilePlanningProvider(source)
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    context = CapabilityContext(
        home=tmp_path,
        source="state_graph",
        peer_id="wx-user",
        sender_id="tester",
        permission_ceiling="write",
        workspace=str(tmp_path),
        trace_id="trace-side-effect-compensate",
    )
    runner = DurableStateGraphRunner(
        home=tmp_path,
        planner_port=ModelCapabilityPlannerPort(
            runtime=runtime,
            capabilities=CapabilityRegistry(
                home=tmp_path,
                project_dir=tmp_path,
                permission_ceiling="write",
            ),
        ),
        executor_port=CapabilityExecutorPort(home=tmp_path, context=context),
        trace_store=TraceStore(tmp_path),
        trace_context=context,
    )
    spec = replace(
        _send_file_spec(_command("import sys; sys.exit(7)")),
        retry_policy=RetryPolicy(max_attempts=1),
    )

    result = await runner.run_async(spec, workspace=tmp_path)

    assert result.terminal_state == LoopTerminalState.FAILED
    staged = Path(result.evidence["capability_result"]["facts"]["outbound_path"])
    assert not staged.exists()
    compensation = result.evidence["side_effect_compensation_results"][0]
    assert compensation["state"] == "compensated"
    compensation_decisions = _loop_decision_payloads_by_tool(
        tmp_path,
        "trace-side-effect-compensate",
        "state_graph.side_effect.compensate",
    )
    assert compensation_decisions
    assert compensation_decisions[0]["evidence"]["side_effect"]["state"] == "compensated"


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
