from __future__ import annotations

import asyncio
import json
import re
import shlex
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from navi.capabilities import CapabilityRegistry
from navi.capabilities_types import CapabilityContext, CapabilityResult
from navi.loop import LoopCheckName, LoopDecisionKind, LoopReason, TraceFailureDomain, TracePhase
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
    WorkspaceMode,
    WorkspacePolicy,
)
from navi.provider import ChatMessage
from navi.runtime import AgentRuntime
from navi.state_graph import (
    CapabilityRecoveryPort,
    CapabilityExecutorPort,
    DurableStateGraphRunner,
    ExecutedCapabilityStep,
    ModelCapabilityPlannerPort,
    PlannedCapabilityStep,
    _planner_verification_failure_text,
    _semantic_checker_attempt_evidence,
    _surface_response_required,
    _transition_loop_decision,
)
from navi.trace import TraceStore
from navi.trace_proxies import TracingPlannerPortProxy
from navi.workspaces import ShadowWorkspaceManager


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
    match = re.search(
        r"<runtime_facts>\s*<!\[CDATA\[(.*?)\]\]>\s*</runtime_facts>",
        turn_input,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def test_semantic_checker_evidence_includes_current_capability_execution() -> None:
    evidence = _semantic_checker_attempt_evidence(
        {
            "attempt_history": [
                {
                    "attempt": 1,
                    "tool": "account.usage",
                    "args": {},
                    "ok": True,
                    "action": "read",
                    "facts": {"remaining_percent": 86.0},
                    "message": "",
                    "error_reason": "",
                    "terminal": False,
                },
                {
                    "attempt": 2,
                    "tool": "custom.presenter",
                    "args": {},
                    "ok": True,
                    "action": "chat",
                    "facts": {},
                    "message": "Current remaining quota is 86%.",
                    "error_reason": "",
                    "terminal": True,
                },
            ]
        }
    )

    assert [item["tool"] for item in evidence] == ["account.usage", "custom.presenter"]
    assert evidence[0]["evidence_authority"] == "declared_capability_observation"
    assert evidence[-1]["evidence_authority"] == "candidate_response_only"
    assert evidence[-1]["action"] == "chat"
    assert evidence[-1]["terminal"] is True


def _spec(command: str, *, timeout: float = 5.0) -> LoopSpec:
    return LoopSpec.from_goal(
        GoalSpec(
            objective="run durable state graph",
            scope=("repo:/tmp/project",),
            acceptance_criteria=("verification command passes",),
            permission_ceiling="read",
        ),
        goal_id="goal-1",
        allowed_capabilities=("shell.run",),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.COMMAND_EXIT_CODE,
                name="verification",
                command=command,
                timeout=TimeoutPolicy(seconds=timeout),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_state_graph_heartbeat_renews_live_execution_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("navi.state_graph.EXECUTION_LEASE_HEARTBEAT_MAX_SECONDS", 0.05)
    runner = DurableStateGraphRunner(
        home=tmp_path / ".navi",
        execution_owner="state-graph:test-heartbeat",
    )
    state = runner.store.create_run(_spec("true"))
    claimed = runner.store.claim_for_execution(
        state.run_id,
        owner=runner.execution_owner,
        lease_seconds=1.0,
    )
    assert claimed is not None
    initial_expiry = claimed.lease_expires_at

    runner._start_execution_lease_heartbeat(run_id=state.run_id, lease_seconds=1.0)
    try:
        await asyncio.sleep(0.12)
        renewed = runner.store.get_run(state.run_id)
        assert renewed is not None
        assert renewed.lease_expires_at > initial_expiry
        assert runner.store.release_expired_execution_leases(
            now=initial_expiry + 0.01
        ) == []
    finally:
        await runner._stop_execution_lease_heartbeat()


def _respond_spec(command: str, *, timeout: float = 5.0) -> LoopSpec:
    return LoopSpec.from_goal(
        GoalSpec(
            objective="ask the user a clarifying question",
            scope=("repo:/tmp/project",),
            acceptance_criteria=("verification command passes",),
            permission_ceiling="read",
        ),
        goal_id="goal-1",
        allowed_capabilities=("respond",),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.COMMAND_EXIT_CODE,
                name="verification",
                command=command,
                timeout=TimeoutPolicy(seconds=timeout),
            ),
        ),
    )


def test_surface_response_requirement_uses_capability_metadata_not_tool_name(
    tmp_path: Path,
) -> None:
    capabilities = CapabilityRegistry(home=tmp_path, project_dir=tmp_path)
    goal = GoalSpec(
        objective="present verified facts",
        scope=("repo:/tmp/project",),
        acceptance_criteria=("verified facts are presented",),
        metadata={"loop_kind": "turn"},
    )
    verification_ladder = (
        VerificationStep(
            kind=VerificationKind.LLM_CHECKER,
            name="semantic checker",
        ),
    )
    surface_spec = LoopSpec.from_goal(
        goal,
        goal_id="goal-surface",
        allowed_capabilities=("ask.user",),
        verification_ladder=verification_ladder,
    )
    facts_only_spec = LoopSpec.from_goal(
        goal,
        goal_id="goal-facts",
        allowed_capabilities=("file.read",),
        verification_ladder=verification_ladder,
    )

    assert _surface_response_required(surface_spec, capabilities) is True
    assert _surface_response_required(facts_only_spec, capabilities) is False


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
        allowed_capabilities=("file.write", "shell.run"),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.COMMAND_EXIT_CODE,
                name="verification",
                command=command,
                timeout=TimeoutPolicy(seconds=timeout),
            ),
        ),
    )


class _ExplodingPlanner:
    async def plan(self, spec, state, *, workspace, evidence):
        del spec, state, workspace, evidence
        raise RuntimeError("provider exploded")


class _UnusedExecutor:
    async def execute(self, step, spec, state, *, workspace):
        del step, spec, state, workspace
        raise AssertionError("executor must not run")


class _StaticResponsePlanner:
    async def plan(self, spec, state, *, workspace, evidence):
        del spec, state, workspace, evidence
        return PlannedCapabilityStep(
            tool="respond",
            permission="read",
            args={"message": "verified response"},
        )


class _StaticResponseExecutor:
    async def execute(self, step, spec, state, *, workspace):
        del step, spec, state, workspace
        return ExecutedCapabilityStep(
            ok=True,
            action="chat",
            facts={},
            message="verified response",
            terminal=True,
        )


class _ExplodingSemanticChecker:
    async def assess(self, spec, state, *, executed, evidence):
        del spec, state, executed, evidence
        raise AssertionError("checker implementation bug")


@pytest.mark.asyncio
async def test_unexpected_planner_exception_discards_shadow_workspace(tmp_path: Path) -> None:
    spec = replace(
        _spec(_command("print('ok')")),
        workspace_policy=WorkspacePolicy(mode=WorkspaceMode.SHADOW),
    )
    runner = DurableStateGraphRunner(
        home=tmp_path / ".navi",
        planner_port=_ExplodingPlanner(),
        executor_port=_UnusedExecutor(),
    )

    with pytest.raises(RuntimeError, match="provider exploded"):
        await runner.run_async(spec, workspace=tmp_path)

    shadows = ShadowWorkspaceManager(tmp_path / ".navi").list_shadows(limit=10)
    assert len(shadows) == 1
    assert shadows[0].status == "discarded"
    assert not Path(shadows[0].shadow_workspace).parent.exists()


@pytest.mark.asyncio
async def test_semantic_checker_programming_error_is_not_provider_failure(
    tmp_path: Path,
) -> None:
    spec = LoopSpec.from_goal(
        GoalSpec(
            objective="present a verified response",
            scope=(f"repo:{tmp_path}",),
            acceptance_criteria=("response is verified",),
        ),
        goal_id="goal-checker-bug",
        allowed_capabilities=("respond",),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.LLM_CHECKER,
                name="semantic checker",
            ),
        ),
    )
    runner = DurableStateGraphRunner(
        home=tmp_path / ".navi",
        planner_port=_StaticResponsePlanner(),
        executor_port=_StaticResponseExecutor(),
        semantic_checker_port=_ExplodingSemanticChecker(),
    )

    with pytest.raises(AssertionError, match="checker implementation bug"):
        await runner.run_async(spec, workspace=tmp_path)


def test_capability_recovery_replans_after_non_retryable_call_failure() -> None:
    spec = _spec(_command("print('ok')"))
    state = LoopRunState(
        run_id="run-non-retryable",
        goal_id=spec.goal_id,
        loop_spec_id=spec.id,
        attempt=1,
    )

    decision = CapabilityRecoveryPort().recover(
        spec,
        state,
        executed=ExecutedCapabilityStep(
            ok=False,
            action="tool",
            facts={"retryable": False, "provider": "search"},
            message="provider configuration will not change during this run",
            error_reason="search_provider_config_error",
        ),
    )

    assert decision.replan_allowed is True
    assert decision.reason_code == "execution_not_retryable"
    assert decision.facts["recovery"]["retryable"] is False


def test_planner_verification_failure_text_extracts_rejection_headline() -> None:
    evidence = {
        "reflection": {
            "replan_allowed": True,
            "reason_code": "semantic_check_failed",
            "facts": {
                "recovery": {
                    "attempt": 8,
                    "blocked": False,
                    "checker_report": {
                        "accepted": False,
                        "blocked": False,
                        "checker_results": [
                            {
                                "name": "objective_check",
                                "passed": False,
                                "reason": "semantic_check_failed",
                                "evidence": {
                                    "evidence_summary": (
                                        "Positions 1 and 4 are not grounded in "
                                        "authoritative capability evidence."
                                    ),
                                },
                            }
                        ],
                    },
                }
            },
        }
    }

    text = _planner_verification_failure_text(evidence)
    assert "Attempt 8 was rejected" in text
    assert "semantic_check_failed" in text
    assert "not grounded in authoritative capability evidence" in text
    assert "Do not repeat" in text


def test_planner_verification_failure_text_empty_when_accepted() -> None:
    evidence = {
        "reflection": {
            "replan_allowed": False,
            "reason_code": "",
            "facts": {
                "recovery": {
                    "attempt": 2,
                    "checker_report": {"accepted": True, "checker_results": []},
                }
            },
        }
    }
    assert _planner_verification_failure_text(evidence) == ""


def test_planner_verification_failure_text_empty_without_reflection() -> None:
    assert _planner_verification_failure_text({}) == ""


@pytest.mark.asyncio
async def test_executor_keeps_scope_workspace_durable_while_using_shadow_project_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    shadow = tmp_path / "shadow"
    repo.mkdir()
    shadow.mkdir()
    captured: dict[str, str] = {}

    async def fake_invoke(
        self,
        name: str,
        args: dict,
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        del name, args, permission
        captured["context_workspace"] = context.workspace
        captured["project_dir"] = str(self.gateway.project_dir)
        return CapabilityResult(ok=True, action="tool", facts={"ok": True})

    monkeypatch.setattr(CapabilityRegistry, "invoke", fake_invoke)
    spec = LoopSpec.from_goal(
        GoalSpec(
            objective="run inside shadow",
            scope=(f"repo:{repo}",),
            acceptance_criteria=("tool executes",),
            permission_ceiling="write",
            metadata={"workspace": str(repo)},
        ),
        goal_id="goal-shadow-scope",
        allowed_capabilities=("tools.list",),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.LLM_CHECKER,
                name="checker",
            ),
        ),
    )

    executed = await CapabilityExecutorPort(
        home=tmp_path,
        context=CapabilityContext(
            home=tmp_path,
            source="weixin",
            peer_id="peer-1",
            sender_id="user-1",
            workspace=str(repo),
        ),
    ).execute(
        PlannedCapabilityStep(tool="tools.list", permission="read", args={}),
        spec,
        LoopRunState(
            run_id="loop-shadow-scope",
            goal_id=spec.goal_id,
            loop_spec_id=spec.id,
        ),
        workspace=shadow,
    )

    assert executed.deterministic_completion_authority is False

    assert executed.ok is True
    assert captured["context_workspace"] == str(repo)
    assert captured["project_dir"] == str(shadow)


def test_checkpoint_restore_rejects_model_fields_missing_from_plan(tmp_path: Path) -> None:
    spec = _spec(_command("print('ok')"))
    runner = DurableStateGraphRunner(home=tmp_path)
    state = runner.store.create_run(spec)
    runner.store.write_checkpoint(
        state.run_id,
        node=LoopNode.EXECUTE,
        inputs={
            "planned_capability": {
                "tool": "shell.run",
                "args": {},
            }
        },
        state=state.to_dict(),
    )

    assert runner._planned_step_from_checkpoint(state.run_id) is None


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
                        "reason": "write requested file in shadow workspace",
                    }
                ]
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "executor", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


class _TraceUsagePlanningProvider(_PlanningProvider):
    def __init__(self) -> None:
        super().__init__()
        self._usage: dict[str, dict] = {}

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        response = await super().complete_for(role, messages, **kwargs)
        self._usage[role] = {
            "role": role,
            "provider": "test-provider",
            "model": "trace-usage-model",
            "input_tokens": 13,
            "output_tokens": 5,
            "prompt_tokens": 13,
            "completion_tokens": 5,
            "total_tokens": 18,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "response": response,
        }
        return response

    def usage_for(self, role: str) -> dict:
        return dict(self._usage.get(role) or {})


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
                    "side_effect_commit_strategy": "deferred",
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
        "commit_strategy": "deferred",
    }


def test_transition_decision_records_connector_outbound_as_external_pause() -> None:
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
            node=LoopNode.PAUSE,
        ),
        checkpoint_id="checkpoint-1",
        condition="resource_or_user_pause",
        terminal_state=LoopTerminalState.PAUSED,
        evidence={"executor": {"action": "connector_outbound", "facts": {}}},
    )

    assert decision.decision == LoopDecisionKind.BLOCKED
    assert decision.reason == LoopReason.EXTERNAL_PAUSE
    assert decision.failure_domain == TraceFailureDomain.NONE
    assert decision.gate_results
    assert decision.gate_results[0].name == LoopCheckName.EXTERNAL_PAUSE
    assert decision.gate_results[0].passed is True
    assert not decision.checker_results


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
        executor_port=CapabilityExecutorPort(
            home=tmp_path,
            context=context,
            sensitive_approval_mode="skip",
        ),
    )

    result = await runner.run_async(
        _write_spec(
            _command("from pathlib import Path; assert Path('app.py').read_text() == 'agent\\n'")
        ),
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
        executor_port=CapabilityExecutorPort(
            home=tmp_path,
            context=context,
            sensitive_approval_mode="skip",
        ),
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
        r'<untrusted_input name="conversation_history">\s*(.*?)\s*</untrusted_input>',
        turn_input,
        re.DOTALL,
    )
    assert conversation is not None
    conversation_text = conversation.group(1)
    assert "Conversation context was compacted before planner intake." in conversation_text
    assert "legacy-start" not in conversation_text
    assert "legacy-tail-marker" not in conversation_text
    assert "recent full marker survives compaction" in conversation_text
    assert "ASSISTANT_CANDIDATE_NON_AUTHORITATIVE" in conversation_text
    assert "Older assistant replies are omitted" in conversation_text
    compaction = _runtime_facts_from_turn_input(turn_input)["conversation_compaction"]
    assert compaction["compacted"] is True
    assert compaction["message_count"] == 18
    assert compaction["omitted_message_count"] == 6
    assert compaction["omitted_older_assistant_message_count"] == 3
    assert compaction["retained_recent_message_count"] == 12
    assert compaction["compacted_character_count"] <= compaction["max_character_count"]


@pytest.mark.asyncio
async def test_background_goal_excludes_non_authoritative_session_history(
    tmp_path: Path,
) -> None:
    provider = _PlanningProvider()
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    session_id = runtime.memory.create_session()
    runtime.memory.add_message(session_id, "user", "codex remaining quota")
    runtime.memory.add_message(
        session_id,
        "assistant",
        "The non-authoritative old remaining quota is 40.0%.",
    )
    spec = _write_spec(_command("print('ok')"))
    spec = replace(
        spec,
        goal=replace(
            spec.goal,
            metadata={
                **spec.goal.metadata,
                "session_id": session_id,
                "execution_mode": "background",
                "task_context": {
                    "progress": {
                        "ambient_history_authoritative": False,
                    }
                },
            },
        ),
    )
    planned = await ModelCapabilityPlannerPort(
        runtime=runtime,
        capabilities=CapabilityRegistry(
            home=tmp_path,
            project_dir=tmp_path,
            permission_ceiling="write",
        ),
    ).plan(
        spec,
        LoopRunState(
            run_id="background-loop",
            goal_id=spec.goal_id,
            loop_spec_id=spec.id,
        ),
        workspace=tmp_path,
        evidence={},
    )

    assert planned.tool == "file.write"
    turn_input = _planner_turn_input(provider)
    assert "<conversation_history>" not in turn_input
    assert "40.0%" not in turn_input
    runtime_facts = _runtime_facts_from_turn_input(turn_input)
    assert runtime_facts["conversation_compaction"] == {
        "consumer": "planner",
        "included": False,
        "policy": "bounded_conversation_context_v1",
        "reason": "background_ambient_history_not_authoritative",
        "session_id": session_id,
    }


@pytest.mark.asyncio
async def test_state_graph_traces_planner_usage(tmp_path: Path) -> None:
    provider = _TraceUsagePlanningProvider()
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
        trace_id="trace-planner-usage",
    )
    trace_store = TraceStore(tmp_path)

    planner_port = ModelCapabilityPlannerPort(
        runtime=runtime,
        capabilities=planner_capabilities,
    )
    planner_port = TracingPlannerPortProxy(planner_port, trace_store, context)

    runner = DurableStateGraphRunner(
        home=tmp_path,
        planner_port=planner_port,
        executor_port=CapabilityExecutorPort(
            home=tmp_path,
            context=context,
            sensitive_approval_mode="skip",
        ),
        trace_store=trace_store,
        trace_context=context,
    )

    result = await runner.run_async(
        _write_spec(_command("from pathlib import Path; assert Path('app.py').exists()")),
        workspace=tmp_path,
    )

    events = trace_store.list_events("trace-planner-usage")
    planner_start = [event for event in events if event.phase == str(TracePhase.PLANNER_CALL_START)]
    planner_results = [event for event in events if event.phase == str(TracePhase.PLANNER_SYSCALL)]
    output = json.loads(planner_results[0].output_json)
    input_payload = json.loads(planner_results[0].input_json)
    assert result.terminal_state == LoopTerminalState.CONVERGED
    assert planner_start
    assert planner_results
    assert output["tool"] == "file.write"
    assert output["usage"] == {
        "role": "planner",
        "provider": "test-provider",
        "model": "trace-usage-model",
        "input_tokens": 13,
        "output_tokens": 5,
        "prompt_tokens": 13,
        "completion_tokens": 5,
        "total_tokens": 18,
    }
    assert "response" not in output["usage"]
    assert "messages" not in output["usage"]
    assert output["llm_response"]
    assert input_payload["prompt"][0]["role"] == "system"


@pytest.mark.asyncio
async def test_state_graph_does_not_write_empty_trace_id_events(tmp_path: Path) -> None:
    provider = _TraceUsagePlanningProvider()
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
    trace_store = TraceStore(tmp_path)
    runner = DurableStateGraphRunner(
        home=tmp_path,
        planner_port=ModelCapabilityPlannerPort(
            runtime=runtime,
            capabilities=planner_capabilities,
        ),
        executor_port=CapabilityExecutorPort(
            home=tmp_path,
            context=context,
            sensitive_approval_mode="skip",
        ),
        trace_store=trace_store,
        trace_context=context,
    )

    result = await runner.run_async(
        _write_spec(_command("from pathlib import Path; assert Path('app.py').exists()")),
        workspace=tmp_path,
    )

    assert result.terminal_state == LoopTerminalState.CONVERGED
    assert trace_store.list_events("") == []


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
        executor_port=CapabilityExecutorPort(
            home=tmp_path,
            context=context,
            sensitive_approval_mode="skip",
        ),
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
    assert '<untrusted_input name="memory_recall">' in turn_input
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
        executor_port=CapabilityExecutorPort(
            home=tmp_path,
            context=context,
            sensitive_approval_mode="skip",
        ),
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
        executor_port=CapabilityExecutorPort(
            home=tmp_path,
            context=context,
            sensitive_approval_mode="skip",
        ),
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
async def test_durable_state_graph_pauses_until_durable_delivery_receipt(
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
        executor_port=CapabilityExecutorPort(
            home=tmp_path,
            context=context,
            sensitive_approval_mode="skip",
        ),
        trace_store=TraceStore(tmp_path),
        trace_context=context,
    )

    result = await runner.run_async(
        _send_file_spec(_command("print('ok')")),
        workspace=tmp_path,
    )

    assert result.terminal_state == LoopTerminalState.PAUSED
    assert result.evidence["capability_result"]["terminal"] is False
    assert result.evidence["capability_result"]["yields_control"] is True
    assert "side_effect_commit_result" not in result.evidence
    delivery = result.evidence["capability_result"]["facts"]["connector_delivery"]
    assert delivery["mode"] == "durable"
    assert Path(delivery["path"]) == source.resolve()
    assert source.exists()
    assert not _loop_decision_payloads_by_tool(
        tmp_path, "trace-side-effect-commit", "state_graph.side_effect.commit"
    )


@pytest.mark.asyncio
async def test_durable_state_graph_does_not_verify_or_compensate_before_delivery_receipt(
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
        executor_port=CapabilityExecutorPort(
            home=tmp_path,
            context=context,
            sensitive_approval_mode="skip",
        ),
        trace_store=TraceStore(tmp_path),
        trace_context=context,
    )
    spec = replace(
        _send_file_spec(_command("import sys; sys.exit(7)")),
        retry_policy=RetryPolicy(max_attempts=1),
    )

    result = await runner.run_async(spec, workspace=tmp_path)

    assert result.terminal_state == LoopTerminalState.PAUSED
    assert source.exists()
    assert "side_effect_compensation_results" not in result.evidence
    assert not _loop_decision_payloads_by_tool(
        tmp_path, "trace-side-effect-compensate", "state_graph.side_effect.compensate"
    )


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
        executor_port=CapabilityExecutorPort(
            home=tmp_path,
            context=context,
            sensitive_approval_mode="skip",
        ),
    )
    spec = replace(
        _write_spec(
            _command("from pathlib import Path; assert Path('app.py').read_text() == 'agent\\n'")
        ),
        retry_policy=RetryPolicy(max_attempts=2),
    )

    result = await runner.run_async(spec, workspace=tmp_path)

    assert result.terminal_state == LoopTerminalState.CONVERGED
    assert provider.calls == 2
    assert result.run_state.attempt == 2
    assert result.run_state.evidence["reason_code"] == ""
    assert result.run_state.evidence["reason"] == ""
    assert result.run_state.evidence["repeat_count"] == 0
    assert result.run_state.evidence["replan_allowed"] is False
    assert result.run_state.evidence["facts"] == {}
    assert result.evidence["reflection"]["replan_allowed"] is True
    assert "recovery_fact" in result.evidence["reflection"]["facts"]
    recovery = result.evidence["reflection"]["facts"]["recovery"]
    assert recovery["blocked"] is False
    assert recovery["failure_domain"] == "verification_failed"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "agent\n"


class _RespondPlanningProvider:
    """Planner that asks the user a clarifying question via respond.

    Mirrors trace 1: the model calls ``respond`` to ask the user a question.
    P1 requires that this message survives the loop and reaches the turn
    result, instead of being silently dropped into ``collected_evidence``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        del kwargs
        self.calls.append((role, messages))
        assert role == "planner"
        return json.dumps(
            {
                "syscalls": [
                    {
                        "tool": "respond",
                        "permission": "read",
                        "args": {
                            "message": "which investor should I send the resume to?",
                            "private_evidence": {"smoke_token": "STATE_GRAPH_PRIVATE_TOKEN"},
                        },
                        "reason": "objective is blocked by missing recipient identity",
                    }
                ]
            }
        )

    def list_roles(self) -> list[str]:
        return ["planner", "executor", "responder"]

    def usage_for(self, role: str) -> dict:
        return {}


@pytest.mark.asyncio
async def test_respond_message_is_preserved_across_loop(tmp_path: Path) -> None:
    """P1: respond message must survive the loop and be promoted to evidence.

    Before P1, ``respond``'s message was stored in
    ``collected_evidence["capability_result"]["message"]`` but never promoted,
    so the user never saw the question. P1 stores it in
    ``responded_message`` so it reaches the turn result even when a later
    capability overwrites ``capability_result``.
    """
    provider = _RespondPlanningProvider()
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    planner_capabilities = CapabilityRegistry(
        home=tmp_path,
        project_dir=tmp_path,
        permission_ceiling="read",
    )
    context = CapabilityContext(
        home=tmp_path,
        source="state_graph",
        peer_id="state_graph",
        sender_id="tester",
        permission_ceiling="read",
        workspace=str(tmp_path),
    )
    runner = DurableStateGraphRunner(
        home=tmp_path,
        planner_port=ModelCapabilityPlannerPort(
            runtime=runtime,
            capabilities=planner_capabilities,
        ),
        executor_port=CapabilityExecutorPort(
            home=tmp_path,
            context=context,
            sensitive_approval_mode="skip",
        ),
    )
    spec = replace(
        _respond_spec(_command("print('ok')")),
        retry_policy=RetryPolicy(max_attempts=1),
    )

    result = await runner.run_async(spec, workspace=tmp_path)

    # The respond message must be preserved in evidence, not dropped.
    assert result.evidence.get("responded_message") == (
        "which investor should I send the resume to?"
    )
    assert result.evidence.get("responded_action") == "chat"
    assert "STATE_GRAPH_PRIVATE_TOKEN" not in result.evidence.get("responded_message")
    assert result.evidence["capability_result"]["facts"]["private_evidence"] == {
        "smoke_token": "STATE_GRAPH_PRIVATE_TOKEN"
    }


def test_execution_args_keep_navi_home_paths_out_of_shadow_remapping(tmp_path) -> None:
    """Connector media lives under .navi which shadow copies exclude.

    A planned command referencing an attachment's real path must keep that
    path (the sandbox ro-binds the real media dir), while ordinary workspace
    paths still remap into the shadow.
    """
    from navi.state_graph import _execution_args_for_workspace

    class _Spec:
        workspace_fields = ("cwd",)
        workspace_policy = "sandbox"
        workspace_scope = "paths"

    logical = tmp_path / "project"
    shadow = tmp_path / ".navi" / "workspace_shadows" / "run-1" / "shadow"
    logical.mkdir(parents=True)
    shadow.mkdir(parents=True)
    attachment = logical / ".navi" / "weixin" / "media" / "inbound" / "resume.pdf"
    attachment.parent.mkdir(parents=True)

    translated = _execution_args_for_workspace(
        _Spec(),
        {
            "cwd": str(logical),
            "command": [
                "pdftotext",
                str(attachment),
                str(logical / "notes.txt"),
            ],
        },
        logical_workspace=str(logical),
        execution_workspace=shadow,
    )

    assert translated["cwd"] == str(shadow)
    assert translated["command"][1] == str(attachment)
    assert translated["command"][2] == str(shadow / "notes.txt")
