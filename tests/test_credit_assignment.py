"""Tests for Causal Credit Assignment Engine and execution graph backpropagation."""
from __future__ import annotations

import json
from pathlib import Path
import time

from navi.credit_assignment import (
    CreditAssignmentEngine,
    compute_terminal_reward,
)
from navi.dynamic_parameters import DynamicParameterRegistry
from navi.loop import TraceFailureDomain, TraceOutcome, TracePhase
from navi.memory.store import MemoryStore
from navi.trace import TraceEvent, TraceStore


def test_compute_terminal_reward() -> None:
    assert compute_terminal_reward("success", "none") == 1.0
    assert compute_terminal_reward("degraded", "none") == 0.2
    assert compute_terminal_reward("failure", "safeguard_policy") == -1.0
    assert compute_terminal_reward("failure", "loop_no_progress") == -0.8
    assert compute_terminal_reward("failure", "planner_or_parser") == -0.7
    assert compute_terminal_reward("failure", "capability_failure") == -0.5
    assert compute_terminal_reward("failure", "provider_no_response") == -0.3


def test_credit_assignment_positive_memory_reinforcement(tmp_path: Path) -> None:
    engine = CreditAssignmentEngine(tmp_path)
    mem_store = MemoryStore(tmp_path)
    item = mem_store.add_item(
        "fact",
        "Python typing uses protocol for structural subtyping",
        source="test",
        status="active",
        confidence=0.70,
        reason="initial",
        provenance="test",
    )

    event = TraceEvent(
        id="ev1",
        trace_id="t1",
        session_id="s1",
        run_id="r1",
        phase=str(TracePhase.PLANNER_SYSCALL),
        source="user",
        peer_id="p1",
        sender_id="u1",
        tool="test_tool",
        model_role="planner",
        ok=True,
        input_json="{}",
        output_json=json.dumps({"used_memory_ids": [item.id]}),
        message="ok",
        created_at=time.time(),
    )

    attributions = engine.backprop_trace(
        trace_id="t1",
        outcome=str(TraceOutcome.SUCCESS),
        failure_domain=str(TraceFailureDomain.NONE),
        events=[event],
    )

    assert len(attributions) == 1
    assert attributions[0].node_type == "memory"
    assert attributions[0].target_id == item.id
    assert attributions[0].reward == 1.0

    updated = mem_store.get_item(item.id)
    assert updated is not None
    # Confidence received positive boost (+0.05 * 1.0 = 0.75)
    assert updated.confidence == 0.75
    assert updated.metadata["last_credit_delta"] == 0.05


def test_credit_assignment_negative_memory_penalty_and_stale(tmp_path: Path) -> None:
    engine = CreditAssignmentEngine(tmp_path)
    mem_store = MemoryStore(tmp_path)
    item = mem_store.add_item(
        "fact",
        "Deprecated API endpoint usage instruction",
        source="test",
        status="active",
        confidence=0.25,
        reason="initial",
        provenance="test",
    )

    event = TraceEvent(
        id="ev2",
        trace_id="t2",
        session_id="s1",
        run_id="r1",
        phase=str(TracePhase.PLANNER_SYSCALL),
        source="user",
        peer_id="p1",
        sender_id="u1",
        tool="test_tool",
        model_role="planner",
        ok=True,
        input_json="{}",
        output_json=json.dumps({"used_memory_ids": [item.id]}),
        message="ok",
        created_at=time.time(),
    )

    attributions = engine.backprop_trace(
        trace_id="t2",
        outcome=str(TraceOutcome.FAILURE),
        failure_domain=str(TraceFailureDomain.CAPABILITY_FAILURE),
        events=[event],
    )

    assert len(attributions) == 1
    assert attributions[0].reward == -0.5

    updated = mem_store.get_item(item.id)
    assert updated is not None
    # 0.25 - (0.10 * 0.5) = 0.20
    assert updated.confidence == 0.20

    # Second failure pushes it below stale threshold (0.20)
    engine.backprop_trace(
        trace_id="t2_bis",
        outcome=str(TraceOutcome.FAILURE),
        failure_domain=str(TraceFailureDomain.CAPABILITY_FAILURE),
        events=[event],
    )
    stale_item = mem_store.get_item(item.id)
    assert stale_item is not None
    # 0.20 - 0.05 = 0.15 < 0.20 -> status becomes stale
    assert stale_item.confidence == 0.15
    assert stale_item.status == "stale"


def test_credit_assignment_parameter_congestion_backoff(tmp_path: Path) -> None:
    engine = CreditAssignmentEngine(tmp_path)
    param_reg = DynamicParameterRegistry(tmp_path)
    initial_retry = param_reg.get("provider_retry_after_seconds")
    assert initial_retry == 15.0

    event = TraceEvent(
        id="ev3",
        trace_id="t3",
        session_id="s1",
        run_id="r1",
        phase=str(TracePhase.PLANNER_CALL_ERROR),
        source="user",
        peer_id="p1",
        sender_id="u1",
        tool="planner.error",
        model_role="planner",
        ok=False,
        input_json="{}",
        output_json="{}",
        message="provider timeout",
        created_at=time.time(),
    )

    attributions = engine.backprop_trace(
        trace_id="t3",
        outcome=str(TraceOutcome.FAILURE),
        failure_domain=str(TraceFailureDomain.PROVIDER_NO_RESPONSE),
        events=[event],
    )

    assert any(a.node_type == "dynamic_parameter" for a in attributions)
    updated_retry = param_reg.get("provider_retry_after_seconds", reload=True)
    assert updated_retry == 17.0


def test_credit_assignment_prompt_layer_blame(tmp_path: Path) -> None:
    engine = CreditAssignmentEngine(tmp_path)
    event = TraceEvent(
        id="ev4",
        trace_id="t4",
        session_id="s1",
        run_id="r1",
        phase=str(TracePhase.PLANNER_CALL_ERROR),
        source="user",
        peer_id="p1",
        sender_id="u1",
        tool="planner.error",
        model_role="planner",
        ok=False,
        input_json="{}",
        output_json="{}",
        message="invalid json schema",
        created_at=time.time(),
    )

    attributions = engine.backprop_trace(
        trace_id="t4",
        outcome=str(TraceOutcome.FAILURE),
        failure_domain=str(TraceFailureDomain.PLANNER_OR_PARSER),
        events=[event],
    )

    assert any(a.node_type == "prompt_layer" and a.target_id == "instructions" for a in attributions)
    all_saved = engine.list_attributions(trace_id="t4")
    assert len(all_saved) >= 1


def test_e2e_trace_store_evaluation_triggers_credit_backprop(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    mem_store = MemoryStore(tmp_path)
    item = mem_store.add_item(
        "fact",
        "Valid knowledge item used in successful query",
        source="test",
        status="active",
        confidence=0.80,
        reason="initial",
        provenance="test",
    )

    # Record trace events
    ev = TraceEvent(
        id="ev_e2e",
        trace_id="trace_e2e_1",
        session_id="sess_1",
        run_id="run_1",
        phase=str(TracePhase.PLANNER_SYSCALL),
        source="user",
        peer_id="p1",
        sender_id="u1",
        tool="echo",
        model_role="planner",
        ok=True,
        input_json="{}",
        output_json=json.dumps({"used_memory_ids": [item.id]}),
        message="done",
        created_at=time.time(),
    )
    store.add_event(
        trace_id=ev.trace_id,
        session_id=ev.session_id,
        run_id=ev.run_id,
        phase=ev.phase,
        source=ev.source,
        peer_id=ev.peer_id,
        sender_id=ev.sender_id,
        tool=ev.tool,
        model_role=ev.model_role,
        ok=ev.ok,
        input_data={},
        output_data={"used_memory_ids": [item.id]},
        message=ev.message,
    )

    # Evaluate trace -> triggers _backprop_credit
    evaluation = store.evaluate_trace("trace_e2e_1")
    assert evaluation.outcome == str(TraceOutcome.SUCCESS)

    # Verify credit attribution recorded in SQLite
    engine = CreditAssignmentEngine(tmp_path)
    attrs = engine.list_attributions(trace_id="trace_e2e_1")
    assert len(attrs) == 1
    assert attrs[0].target_id == item.id
    assert attrs[0].reward == 1.0

    # Verify memory item confidence boosted
    reloaded_item = mem_store.get_item(item.id)
    assert reloaded_item is not None
    assert reloaded_item.confidence == 0.85


def test_credit_assignment_temporal_difference_discounting(tmp_path: Path) -> None:
    engine = CreditAssignmentEngine(tmp_path)
    mem_store = MemoryStore(tmp_path)
    item_early = mem_store.add_item(
        "fact",
        "Early context fact retrieved at step 0",
        source="test",
        status="active",
        confidence=0.50,
        reason="initial",
        provenance="test",
    )
    item_late = mem_store.add_item(
        "fact",
        "Late context fact retrieved at step 1",
        source="test",
        status="active",
        confidence=0.50,
        reason="initial",
        provenance="test",
    )

    ev_early = TraceEvent(
        id="ev_step0",
        trace_id="td_trace",
        session_id="s1",
        run_id="r1",
        phase=str(TracePhase.PLANNER_SYSCALL),
        source="user",
        peer_id="p1",
        sender_id="u1",
        tool="tool0",
        model_role="planner",
        ok=True,
        input_json="{}",
        output_json=json.dumps({"used_memory_ids": [item_early.id]}),
        message="step 0",
        created_at=time.time(),
    )
    ev_late = TraceEvent(
        id="ev_step1",
        trace_id="td_trace",
        session_id="s1",
        run_id="r1",
        phase=str(TracePhase.PLANNER_SYSCALL),
        source="user",
        peer_id="p1",
        sender_id="u1",
        tool="tool1",
        model_role="planner",
        ok=False,
        input_json="{}",
        output_json=json.dumps({"used_memory_ids": [item_late.id]}),
        message="step 1 failed",
        created_at=time.time() + 1.0,
    )

    attributions = engine.backprop_trace(
        trace_id="td_trace",
        outcome=str(TraceOutcome.FAILURE),
        failure_domain=str(TraceFailureDomain.CAPABILITY_FAILURE),
        events=[ev_early, ev_late],
    )

    assert len(attributions) == 2
    attr_by_target = {a.target_id: a for a in attributions}

    assert attr_by_target[item_late.id].reward == -0.5
    assert attr_by_target[item_early.id].reward == -0.425

    updated_early = mem_store.get_item(item_early.id)
    updated_late = mem_store.get_item(item_late.id)
    assert updated_early is not None
    assert updated_late is not None
    assert updated_late.confidence == 0.45
    assert updated_early.confidence == 0.4575
    assert updated_early.confidence > updated_late.confidence

