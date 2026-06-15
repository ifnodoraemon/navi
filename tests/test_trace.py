from __future__ import annotations

import json

from navi.db import connect
from navi.trace import TraceStore


def test_trace_store_redacts_sensitive_fields_and_lists_events(tmp_path):
    store = TraceStore(tmp_path)
    trace_id = "trace-redaction"

    store.add_event(
        trace_id=trace_id,
        phase="planner.syscall",
        input_data={
            "api_key": "secret",
            "nested": {"password": "pw"},
            "items": [{"token": "tok"}, {"safe": "value"}],
        },
        output_data={"approval_code": "123456", "safe": "ok"},
        message="planned",
    )

    events = store.list_events(trace_id)

    assert store.list_trace_ids() == [trace_id]
    assert events[0].phase == "planner.syscall"
    assert json.loads(events[0].input_json) == {
        "api_key": "[redacted]",
        "items": [{"token": "[redacted]"}, {"safe": "value"}],
        "nested": {"password": "[redacted]"},
    }
    assert json.loads(events[0].output_json)["approval_code"] == "[redacted]"


def test_trace_store_reinitializes_schema_drift(tmp_path):
    with connect(tmp_path / "traces.db") as conn:
        conn.execute(
            """
            CREATE TABLE trace_events (
                id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                task_id TEXT NOT NULL,
                tool TEXT NOT NULL,
                model_role TEXT NOT NULL,
                ok INTEGER NOT NULL,
                input_json TEXT NOT NULL,
                output_json TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )

    store = TraceStore(tmp_path)
    event = store.add_event(trace_id="trace-current", phase="turn.start")

    assert event.trace_id == "trace-current"
    with connect(tmp_path / "traces.db") as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(trace_events)").fetchall()]
    assert "session_id" in columns
    assert "task_id" not in columns


def test_trace_store_evaluates_failure_domains_and_budget_degradation(tmp_path):
    store = TraceStore(tmp_path)

    store.add_event(
        trace_id="planner-failure",
        phase="planner.syscall",
        ok=False,
        tool="provider.config",
        message="invalid model output",
    )
    planner_eval = store.evaluate_trace("planner-failure")

    store.add_event(
        trace_id="tool-failure",
        phase="capability.result",
        ok=False,
        tool="delegate.run",
        message="missing execution grant",
    )
    tool_eval = store.evaluate_trace("tool-failure")

    store.add_event(
        trace_id="safeguard-failure",
        phase="capability.result",
        ok=False,
        tool="shell.run",
        output_data={
            "facts": {
                "hook_decision": {
                    "hook": "remote_safe_policy",
                    "decision": "block",
                    "reason": "remote connector cannot run shell",
                }
            }
        },
        message="remote connector cannot run shell",
    )
    safeguard_eval = store.evaluate_trace("safeguard-failure")

    store.add_event(
        trace_id="runtime-failure",
        phase="turn.start",
        ok=False,
        message="session init failed",
    )
    runtime_eval = store.evaluate_trace("runtime-failure")

    store.add_event(
        trace_id="budget",
        phase="agent.role_result",
        model_role="responder",
        message="responder synthesized response",
    )
    store.add_event(
        trace_id="budget",
        phase="turn.final",
        output_data={"budget_exhausted": True},
    )
    budget_eval = store.evaluate_trace("budget")

    store.add_event(
        trace_id="completion-verify",
        phase="completion.verify",
        ok=False,
        tool="final.answer",
        message="task is still pending",
    )
    store.add_event(
        trace_id="completion-verify",
        phase="recovery.plan",
        ok=True,
        output_data={"recommended": "continue"},
    )
    completion_eval = store.evaluate_trace("completion-verify")

    store.add_event(trace_id="planner-no-response", phase="turn.start")
    store.add_event(trace_id="planner-no-response", phase="planner.call.start")
    no_response_eval = store.evaluate_trace("planner-no-response")
    listed_completion_evals = store.list_evaluations("completion-verify")
    listed_all_evals = store.list_evaluations()

    store.add_event(
        trace_id="pending-risk",
        phase="capability.result",
        ok=True,
        tool="delegate.spawn",
        output_data={"facts": {"entity_type": "delegation_run", "run_id": "task-1", "status": "pending"}},
    )
    store.add_event(
        trace_id="pending-risk",
        phase="turn.final",
        ok=True,
        message="done",
    )
    pending_eval = store.evaluate_trace("pending-risk")

    missing_eval = store.evaluate_trace("missing")

    assert planner_eval.outcome == "failure"
    assert planner_eval.failure_domain == "prompt_or_provider_parser"
    assert json.loads(planner_eval.evidence_json)["first_failure_tool"] == "provider.config"
    assert tool_eval.failure_domain == "tool_or_capability"
    assert safeguard_eval.outcome == "failure"
    assert safeguard_eval.failure_domain == "safeguard_policy"
    assert "safeguard hook decision" in safeguard_eval.diagnostic
    assert runtime_eval.failure_domain == "runtime"
    assert budget_eval.outcome == "degraded"
    assert budget_eval.failure_domain == "planning_budget"
    assert json.loads(budget_eval.evidence_json)["agent_role_results"][0]["model_role"] == "responder"
    assert completion_eval.outcome == "failure"
    assert completion_eval.failure_domain == "completion_verifier"
    assert no_response_eval.outcome == "failure"
    assert no_response_eval.failure_domain == "provider_or_planner_no_response"
    assert listed_completion_evals[0].id == completion_eval.id
    assert any(evaluation.id == completion_eval.id for evaluation in listed_all_evals)
    completion_evidence = json.loads(completion_eval.evidence_json)
    assert completion_evidence["recovery_plan_recorded"] is True
    assert completion_evidence["recovery_recommended"] == "continue"
    assert pending_eval.outcome == "degraded"
    assert pending_eval.failure_domain == "completion_verifier_gap"
    assert json.loads(pending_eval.evidence_json)["pending_run_completion_risk"] is True
    assert missing_eval.outcome == "unknown"
    assert missing_eval.failure_domain == "trace_missing"
