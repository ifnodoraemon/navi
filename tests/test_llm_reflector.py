"""Tests for the isolated LLM semantic checker in the durable StateGraph.

These tests verify the semantic checker loop: when a goal has no command
verification, an isolated checker role judges final capability evidence. The
deterministic checker consumes that evidence and the StateGraph either retries,
converges, or blocks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.lifecycle import Resolution
from navi.loop_contracts import (
    GoalSpec,
    LoopRunState,
    LoopSpec,
    LoopTerminalState,
    VerificationKind,
    VerificationStep,
)
from navi.provider import ChatMessage
from navi.runtime import AgentRuntime
from navi.runs import RunStore
from navi.state_graph import ExecutedCapabilityStep, LLMSemanticCheckerPort
from navi.trace import TraceStore


class _ScriptedProvider:
    """A provider that returns scripted responses per role.

    - planner: returns a single syscall (the next capability to try)
    - checker: returns the isolated semantic judgement
    - responder: returns a natural language reply synthesized from facts
    """

    def __init__(
        self,
        *,
        planner_syscalls: list[dict],
        checker_decisions: list[dict],
    ) -> None:
        self._planner_syscalls = list(planner_syscalls)
        self._checker_decisions = list(checker_decisions)
        self.calls: list[str] = []
        self.messages: dict[str, list[ChatMessage]] = {}

    async def complete_for(self, role: str, messages: list[ChatMessage], **kwargs) -> str:
        self.calls.append(role)
        self.messages[role] = messages
        if role == "planner":
            syscall = self._planner_syscalls.pop(0)
            return json.dumps({"syscalls": [syscall]})
        if role == "checker":
            decision = self._checker_decisions.pop(0)
            return json.dumps(decision)
        # responder / default — synthesize from the facts payload
        return "I'll handle that for you."

    def list_roles(self) -> list[str]:
        return ["planner", "checker", "responder"]

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
        "reason": f"write {path}",
    }


@pytest.mark.asyncio
async def test_semantic_checker_uses_conversation_only_to_resolve_elliptical_turn(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider(
        planner_syscalls=[],
        checker_decisions=[
            {
                "passed": True,
                "evidence_summary": "the current result answers the contextual request",
            }
        ],
    )
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    session_id = runtime.memory.create_session()
    runtime.memory.add_message(session_id, "user", "我们是不是应该换个搜索引擎呢")
    runtime.memory.add_message(
        session_id,
        "assistant",
        "可以切换到另一个搜索引擎继续搜索，也可以查看官方文档。",
    )
    runtime.memory.add_message(session_id, "user", "可以")
    spec = LoopSpec.from_goal(
        GoalSpec(
            objective="可以",
            scope=(f"repo:{tmp_path}",),
            metadata={
                "session_id": session_id,
                "execution_mode": "foreground",
            },
        ),
        goal_id="elliptical-turn",
        allowed_capabilities=("web.search", "respond"),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.LLM_CHECKER,
                name="objective_check",
                evidence_key="semantic_checker_result",
            ),
        ),
    )

    decision = await LLMSemanticCheckerPort(runtime=runtime).assess(
        spec,
        LoopRunState(
            run_id="elliptical-run",
            goal_id=spec.goal_id,
            loop_spec_id=spec.id,
        ),
        executed=ExecutedCapabilityStep(
            ok=True,
            action="tool",
            facts={"provider": "alternate-search", "result_count": 3},
        ),
        evidence={},
    )

    assert decision.passed is True
    checker_input = json.loads(provider.messages["checker"][-1].content)
    context = checker_input["conversation_context"]
    assert context["included"] is True
    assert context["authority"] == "semantic_context_only"
    assert context["reason"] == "foreground_conversation_continuity"
    assert context["consumer"] == "semantic_checker"
    assert "ASSISTANT_CANDIDATE_NON_AUTHORITATIVE" in context["transcript"]
    assert "我们是不是应该换个搜索引擎呢" in context["transcript"]
    assert "可以" in context["transcript"]
    assert "task_completion" in context["does_not_establish"]
    evaluation_contract = checker_input["evaluation_contract"]
    assert evaluation_contract["scope"] == "capability_evidence_before_candidate_presentation"
    assert evaluation_contract["presentation_semantics"]["candidate_copy_present"] is False
    assert (
        evaluation_contract["presentation_semantics"][
            "conversation_assistant_is_current_candidate"
        ]
        is False
    )


@pytest.mark.asyncio
async def test_semantic_checker_receives_authoritative_schedule_trigger_facts(
    tmp_path: Path,
) -> None:
    trigger_facts = {
        "type": "scheduled_occurrence",
        "occurrence_number": 2,
        "prior_occurrences": [
            {
                "accepted_result_text": "Lesson 1: foundations",
                "accepted_result": {
                    "body": "Lesson 1: foundations",
                    "body_provenance": "state_graph.evidence.responded_message",
                },
                "delivery": {"state_transition": "delivered"},
            }
        ],
    }
    task_context = {
        "lineage": {
            "id": "daily-topic-lineage",
            "kind": "recurring_goal",
        },
        "progress": {
            "scope": "lineage",
            "sequence_number": 2,
            "authority": "same_lineage_authoritative_prior_items",
            "authoritative_prior_items": trigger_facts["prior_occurrences"],
            "ambient_history_authoritative": False,
        },
        "delivery": {
            "stage": "post_semantic_acceptance_outbox",
            "transport_receipt_available": False,
        },
    }
    spec = LoopSpec.from_goal(
        GoalSpec(
            objective="teach a progressive daily topic",
            scope=(f"repo:{tmp_path}",),
            acceptance_criteria=("respond for the current occurrence",),
            metadata={"trigger_facts": trigger_facts, "task_context": task_context},
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
    provider = _ScriptedProvider(
        planner_syscalls=[],
        checker_decisions=[
            {
                "passed": True,
                "should_continue": False,
                "evidence_summary": "current occurrence advanced the lesson",
            }
        ],
    )

    decision = await LLMSemanticCheckerPort(
        runtime=AgentRuntime(home=tmp_path, provider=provider)
    ).assess(
        spec,
        LoopRunState(
            run_id="run-2",
            goal_id=spec.goal_id,
            loop_spec_id=spec.id,
        ),
        executed=ExecutedCapabilityStep(
            ok=True,
            action="respond",
            facts={"responded_message": "Lesson 2: supervised learning"},
        ),
        evidence={
            "attempt_history": [
                {
                    "attempt": 1,
                    "tool": "web.search",
                    "args": {
                        "query": "lesson progression",
                        "token": "secret-token",
                    },
                    "ok": True,
                    "action": "tool",
                    "facts": {"result_count": 2, "provider": "test"},
                    "message": "sources found",
                    "error_reason": "",
                    "terminal": False,
                },
                {
                    "attempt": 2,
                    "tool": "respond",
                    "args": {"message": "Lesson 2: supervised learning"},
                    "ok": True,
                    "action": "respond",
                    "facts": {},
                    "message": "Lesson 2: supervised learning",
                    "error_reason": "",
                    "terminal": True,
                },
            ]
        },
    )

    assert decision.passed is True
    checker_input = json.loads(provider.messages["checker"][-1].content)
    assert checker_input["trigger_facts"] == trigger_facts
    assert "task_lineage" not in checker_input
    assert checker_input["task_context"]["lineage"] == {
        "id": "daily-topic-lineage",
        "kind": "recurring_goal",
        "current_goal_id": "current-occurrence",
        "parent_goal_id": "",
    }
    assert (
        checker_input["task_context"]["progress"]["authority"]
        == "same_lineage_authoritative_prior_items"
    )
    assert checker_input["task_context"]["progress"]["sequence_number"] == 2
    assert (
        checker_input["task_context"]["progress"]["authoritative_prior_items"]
        == trigger_facts["prior_occurrences"]
    )
    prior_result_facts = checker_input["task_context"]["progress"]["prior_result_facts"]
    assert prior_result_facts["item_count"] == 1
    assert prior_result_facts["items"][0]["canonical_sha256_16"]
    comparison = checker_input["task_context"]["progress"]["current_result_comparison"]
    assert comparison["current_result"]["present"] is True
    assert comparison["exact_duplicate_prior_count"] == 0
    assert comparison["latest_prior_exact_duplicate"] is False
    assert comparison["max_similarity"] < 1.0
    assert checker_input["current_time"]["unix"] > 0
    assert checker_input["current_time"]["iso"]
    assert "utc_offset" in checker_input["current_time"]
    assert checker_input["conversation_context"] == {
        "authority": "semantic_context_only",
        "does_not_establish": [
            "capability_facts",
            "task_completion",
            "external_effects",
            "connector_delivery",
        ],
        "establishes": ["conversation_referents", "elliptical_turn_meaning"],
        "included": False,
        "policy": "bounded_conversation_context_v1",
        "reason": "no_session_context",
    }
    assert "delivery" not in checker_input["task_context"]
    assert checker_input["evaluation_contract"] == {
        "scope": "candidate_semantics_before_external_transport",
        "evaluates": [
            "objective_coverage",
            "acceptance_criteria",
            "grounding_in_authoritative_capability_facts",
            "contradiction_absence",
        ],
        "does_not_evaluate": [
            "connector_transport",
            "external_delivery_receipt",
        ],
        "presentation_semantics": {
            "candidate_copy_role": "proposed_user_facing_communication",
            "candidate_copy_present": True,
            "candidate_copy_source": "capability.facts.responded_message",
            "conversation_assistant_is_current_candidate": False,
            "communication_obligation_rule": (
                "judge whether the candidate copy communicates the requested grounded "
                "content within this pre-transport scope"
            ),
            "transport_proof_rule": (
                "never require an outbox entry, connector send result, or external "
                "delivery receipt in this check"
            ),
        },
        "downstream_transport_evidence_unavailable_by_design": [
            "outbox_entry",
            "connector_send_result",
            "external_delivery_receipt",
        ],
        "transport_receipt_required_for_this_check": False,
        "transport_stage_after_acceptance": True,
        "conversation_context_authority": "resolve_referents_only",
    }
    supporting = checker_input["observed_capability_evidence"]
    assert [item["tool"] for item in supporting] == ["web.search", "respond"]
    assert "result_count" in supporting[0]["facts_json"]
    assert "secret-token" not in supporting[0]["args_json"]
    assert "[REDACTED]" in supporting[0]["args_json"]
    assert "reason" not in supporting[0]
    assert supporting[-1]["action"] == "respond"
    assert supporting[-1]["terminal"] is True


@pytest.mark.asyncio
async def test_semantic_checker_receives_exact_duplicate_prior_result_facts(
    tmp_path: Path,
) -> None:
    repeated_text = "Lesson 1: foundations"
    prior_occurrences = [
        {
            "goal_id": "occurrence-1",
            "run_id": "run-1",
            "accepted_result_text": repeated_text,
            "accepted_result": {
                "body": repeated_text,
                "body_provenance": "state_graph.evidence.responded_message",
            },
        }
    ]
    task_context = {
        "lineage": {
            "id": "daily-topic-lineage",
            "kind": "recurring_goal",
        },
        "progress": {
            "scope": "lineage",
            "sequence_number": 2,
            "authority": "same_lineage_authoritative_prior_items",
            "authoritative_prior_items": prior_occurrences,
            "ambient_history_authoritative": False,
        },
    }
    spec = LoopSpec.from_goal(
        GoalSpec(
            objective="teach a progressive daily topic with fresh content",
            scope=(f"repo:{tmp_path}",),
            acceptance_criteria=(
                "respond for the current occurrence without duplicating prior output",
            ),
            metadata={"task_context": task_context},
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
    provider = _ScriptedProvider(
        planner_syscalls=[],
        checker_decisions=[
            {
                "passed": False,
                "evidence_summary": "candidate repeats the prior accepted result",
            }
        ],
    )

    decision = await LLMSemanticCheckerPort(
        runtime=AgentRuntime(home=tmp_path, provider=provider)
    ).assess(
        spec,
        LoopRunState(
            run_id="run-2",
            goal_id=spec.goal_id,
            loop_spec_id=spec.id,
        ),
        executed=ExecutedCapabilityStep(
            ok=True,
            action="respond",
            facts={},
            message=repeated_text,
        ),
        evidence={},
    )

    assert decision.passed is False
    checker_input = json.loads(provider.messages["checker"][-1].content)
    comparison = checker_input["task_context"]["progress"]["current_result_comparison"]
    assert comparison["current_result"]["source"] == "capability.message"
    assert comparison["exact_duplicate_prior_count"] == 1
    assert comparison["latest_prior_exact_duplicate"] is True
    assert comparison["max_similarity"] == 1.0
    assert comparison["prior_comparisons"][0]["run_id"] == "run-1"


async def _run_goal_with_approvals(
    registry,
    args: dict,
    *,
    home: Path,
):
    context = _context(home)
    result = await registry.invoke(
        "goal.open",
        args,
        permission="prepare",
        context=context,
    )
    goal_id = result.facts["goal_id"]
    while True:
        runs = RunStore(home)
        if "loop_terminal_state" not in result.facts:
            approval = runs.pending_approval_for_run(result.run_id)
            assert approval is not None
            resolved = await registry.invoke(
                "approval.resolve",
                {"decision": "approve", "code": approval.code},
                permission="prepare",
                context=context,
            )
            assert resolved.ok is True
            result = await registry.invoke(
                "goal.resume",
                {"goal_id": goal_id, "workspace": str(home)},
                permission="prepare",
                context=context,
            )
            continue
        if result.facts["loop_terminal_state"] != LoopTerminalState.WAITING_APPROVAL:
            break
        approval = runs.pending_approval_for_run(result.run_id)
        assert approval is not None
        runs.resolve_approval(
            approval.id,
            decision="approve",
            resolved_by="tester",
        )
        result = await registry.invoke(
            "goal.resume",
            {"goal_id": goal_id, "workspace": str(home)},
            permission="prepare",
            context=context,
        )
    return result


@pytest.mark.asyncio
async def test_semantic_checker_converges_when_goal_achieved(tmp_path: Path) -> None:
    """The isolated checker judges passed=true and converges."""
    provider = _ScriptedProvider(
        planner_syscalls=[_file_write_syscall("done.txt", "ok")],
        checker_decisions=[
            {
                "passed": True,
                "should_continue": False,
                "evidence_summary": "done.txt was written",
            }
        ],
    )
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=runtime,
    )

    result = await _run_goal_with_approvals(
        registry,
        {
            "objective": "write done.txt",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["file.write"],
            "verification_command": "",
        },
        home=tmp_path,
    )

    assert result.ok is True
    assert result.facts["loop_terminal_state"] == LoopTerminalState.CONVERGED
    assert (tmp_path / "done.txt").read_text(encoding="utf-8") == "ok"
    assert "checker" in provider.calls
    trace_events = TraceStore(tmp_path).list_events(result.run_id)
    assert {event.model_role for event in trace_events} >= {"planner", "checker"}
    assert all(event.trace_id == result.run_id for event in trace_events)


@pytest.mark.asyncio
async def test_semantic_checker_sees_capability_facts_without_internal_completion_gate(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider(
        planner_syscalls=[],
        checker_decisions=[
            {
                "passed": True,
                "evidence_summary": "current account usage is present",
            }
        ],
    )
    spec = LoopSpec.from_goal(
        GoalSpec(
            objective="report the current account usage",
            scope=(f"repo:{tmp_path}",),
        ),
        goal_id="usage-goal",
        allowed_capabilities=("account.usage", "respond"),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.LLM_CHECKER,
                name="objective_check",
                evidence_key="semantic_checker_result",
            ),
        ),
    )

    decision = await LLMSemanticCheckerPort(
        runtime=AgentRuntime(home=tmp_path, provider=provider)
    ).assess(
        spec,
        LoopRunState(
            run_id="usage-loop",
            goal_id=spec.goal_id,
            loop_spec_id=spec.id,
        ),
        executed=ExecutedCapabilityStep(
            ok=True,
            action="account.usage",
            facts={
                "available": True,
                "plan": "Plus",
                "quota_remaining_percent": 78.0,
                "source": "usage_api",
            },
            deterministic_completion_authority=False,
        ),
        evidence={},
    )

    assert decision.passed is True
    checker_input = json.loads(provider.messages["checker"][-1].content)
    assert checker_input["acceptance_criteria"] == []
    assert checker_input["last_capability"]["facts"]["quota_remaining_percent"] == 78.0
    assert "deterministic_completion_authority" not in checker_input["last_capability"]


@pytest.mark.asyncio
async def test_semantic_checker_retries_then_converges(tmp_path: Path) -> None:
    """The checker says should_continue=true, then passes on attempt 2."""
    provider = _ScriptedProvider(
        planner_syscalls=[
            _file_write_syscall("v1.txt", "first"),
            _file_write_syscall("v2.txt", "second"),
        ],
        checker_decisions=[
            {
                "passed": False,
                "should_continue": True,
                "evidence_summary": "first file does not satisfy the objective",
            },
            {
                "passed": True,
                "should_continue": False,
                "evidence_summary": "v2.txt was written",
            },
        ],
    )
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=runtime,
    )

    result = await _run_goal_with_approvals(
        registry,
        {
            "objective": "write a file then verify",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["file.write"],
            "verification_command": "",
        },
        home=tmp_path,
    )

    assert result.ok is True
    assert result.facts["loop_terminal_state"] == LoopTerminalState.CONVERGED
    # both planner calls happened (2 iterations)
    assert provider.calls.count("planner") == 2
    assert provider.calls.count("checker") == 2
    assert (tmp_path / "v2.txt").read_text(encoding="utf-8") == "second"


@pytest.mark.asyncio
async def test_semantic_checker_verdict_does_not_own_loop_control(tmp_path: Path) -> None:
    """A checker verdict cannot choose the next action or force an immediate stop."""
    provider = _ScriptedProvider(
        planner_syscalls=[_file_write_syscall("v1.txt", "first")] * 4,
        checker_decisions=[
            {
                "passed": False,
                "should_continue": False,
                "evidence_summary": "requested details are missing",
            }
        ]
        * 4,
    )
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=runtime,
    )

    result = await _run_goal_with_approvals(
        registry,
        {
            "objective": "write a file then verify",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["file.write"],
            "verification_command": "",
        },
        home=tmp_path,
    )

    assert result.ok is False
    assert result.facts["loop_terminal_state"] == LoopTerminalState.FAILED
    assert result.facts["resolution"] == Resolution.FAILED
    # The checker was observed four times; it did not terminate the loop. The
    # next planner-provider failure is preserved as a terminal structured fact
    # instead of being repeated by the runtime.
    assert provider.calls.count("planner") == 5
    assert provider.calls.count("checker") == 4


@pytest.mark.asyncio
async def test_semantic_checker_terminates_at_max_attempts(tmp_path: Path) -> None:
    """The loop terminates at max_attempts even if the LLM keeps saying continue.

    The scripted provider has 12 planner syscalls and 12 checker decisions
    (more than the default max_attempts=10). If the loop did NOT terminate at
    max_attempts, it would exhaust the scripted responses and raise IndexError.
    So this test both verifies termination AND that the loop doesn't spin
    forever on a "should_continue=true" LLM.
    """
    continue_decision = {
        "passed": False,
        "should_continue": True,
        "evidence_summary": "not enough evidence yet",
    }
    provider = _ScriptedProvider(
        planner_syscalls=[_file_write_syscall(f"v{i}.txt", f"iter-{i}") for i in range(12)],
        checker_decisions=[continue_decision] * 12,
    )
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        runtime=runtime,
    )

    result = await _run_goal_with_approvals(
        registry,
        {
            "objective": "write a file then verify",
            "workspace": str(tmp_path),
            "allowed_capabilities": ["file.write"],
            "verification_command": "",
            "timeout_seconds": 5,
        },
        home=tmp_path,
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
