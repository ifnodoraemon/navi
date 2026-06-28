from __future__ import annotations

import json

from fastapi.testclient import TestClient

from navi.api import create_app
from navi.db import connect
from navi.loop import LoopCheckName, LoopDecisionKind, LoopReason, TraceFailureDomain
from navi.trace import LoopCheckResult, LoopDecision, TraceStore


def test_trace_store_redacts_sensitive_fields_and_lists_events(tmp_path):
    store = TraceStore(tmp_path)
    trace_id = "trace-redaction"

    store.add_event(
        trace_id=trace_id,
        phase="planner.syscall",
        session_id="session-redaction",
        input_data={
            "api_key": "secret",
            "nested": {"password": "pw"},
            "items": [{"token": "tok"}, {"safe": "value"}],
        },
        output_data={"approval_code": "123456", "safe": "ok"},
        message="planned",
    )
    store.add_loop_decision(
        trace_id=trace_id,
        decision=LoopDecision(
            decision="finalize",
            reason="redaction_test",
            evidence={"api_key": "secret"},
        ),
    )

    events = store.list_events(trace_id)
    decisions = store.list_loop_decisions(trace_id)
    runs = store.list_run_views(trace_id)

    assert store.list_trace_ids() == [trace_id]
    assert events[0].phase == "planner.syscall"
    assert json.loads(events[0].input_json) == {
        "api_key": "[redacted]",
        "items": [{"token": "[redacted]"}, {"safe": "value"}],
        "nested": {"password": "[redacted]"},
    }
    assert json.loads(events[0].output_json)["approval_code"] == "[redacted]"
    assert len(decisions) == 1
    assert json.loads(decisions[0].output_json)["evidence"]["api_key"] == "[redacted]"
    assert runs[0].id == trace_id
    assert runs[0].run_type == "chain"
    assert runs[0].thread_id == "session-redaction"
    assert runs[0].metadata["event_count"] == 2
    assert runs[1].run_type == "llm"
    assert runs[1].inputs["api_key"] == "[redacted]"
    assert runs[2].name == "loop.decision"
    assert runs[2].parent_run_id == trace_id
    assert runs[2].feedback == {}


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
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        ]
    assert "session_id" in columns
    assert "task_id" not in columns
    assert "trace_runs" not in tables


def test_trace_decisions_api_returns_structured_loop_decisions(tmp_path):
    store = TraceStore(tmp_path)
    store.add_loop_decision(
        trace_id="trace-api",
        decision=LoopDecision(
            decision=LoopDecisionKind.FINALIZE,
            reason=LoopReason.TERMINAL_RESULT,
            checker_results=(
                LoopCheckResult(
                    name=LoopCheckName.TERMINAL_RESULT,
                    passed=True,
                    reason="terminal action chat",
                ),
            ),
        ),
    )
    app = create_app(tmp_path)
    api_key = (tmp_path / "api_key").read_text(encoding="utf-8").strip()
    client = TestClient(app)

    response = client.get(
        "/v1/traces/trace-api/decisions",
        headers={"X-API-Key": api_key},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    decisions = payload["data"]["loop_decisions"]
    assert len(decisions) == 1
    assert decisions[0]["decision"]["decision"] == "finalize"
    assert decisions[0]["decision"]["checker_results"][0]["name"] == "terminal_result"

    trace_response = client.get(
        "/v1/traces/trace-api",
        headers={"X-API-Key": api_key},
    )
    assert trace_response.status_code == 200
    trace_payload = trace_response.json()
    assert trace_payload["data"]["runs"][0]["id"] == "trace-api"
    assert trace_payload["data"]["runs"][0]["feedback"] == {}

    runs_response = client.get(
        "/v1/traces/trace-api/runs",
        headers={"X-API-Key": api_key},
    )
    assert runs_response.status_code == 200
    runs_payload = runs_response.json()
    assert runs_payload["data"]["runs"][0]["run_type"] == "chain"
    assert "thread_id" in runs_payload["data"]["runs"][0]


def test_trace_store_evaluates_failure_domains(tmp_path):
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
        trace_id="completion-verify",
        phase="loop.check",
        ok=False,
        tool="final.answer",
        message="task is still pending",
    )
    store.add_event(
        trace_id="completion-verify",
        phase="loop.recovery",
        ok=True,
        output_data={
            "blocked": True,
            "details": {"blocked_entity_type": "delegation_run", "run_status": "pending"},
        },
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

    store.add_loop_decision(
        trace_id="loop-failed",
        decision=LoopDecision(
            decision="failed",
            reason="planner_parse_error",
            failure_domain=TraceFailureDomain.PLANNER_OR_PARSER,
            checker_results=(
                LoopCheckResult(
                    name="planner_result",
                    passed=False,
                    severity="error",
                    reason="invalid structured output",
                ),
            ),
        ),
    )
    loop_failed_eval = store.evaluate_trace("loop-failed")

    store.add_loop_decision(
        trace_id="loop-approval",
        decision=LoopDecision(
            decision="pause_for_approval",
            reason="approval_already_pending",
            failure_domain=TraceFailureDomain.APPROVAL_LOOP,
            gate_results=(
                LoopCheckResult(
                    name="approval_gate",
                    passed=False,
                    severity="warning",
                    reason="existing approval is still pending",
                ),
            ),
        ),
    )
    loop_approval_eval = store.evaluate_trace("loop-approval")

    store.add_loop_decision(
        trace_id="loop-converged",
        decision=LoopDecision(
            decision="converged",
            reason="repeated_progress_signature",
            failure_domain=TraceFailureDomain.LOOP_NO_PROGRESS,
            gate_results=(
                LoopCheckResult(
                    name="no_progress_gate",
                    passed=False,
                    severity="warning",
                    reason="same capability result signature was observed twice",
                ),
            ),
        ),
    )
    loop_converged_eval = store.evaluate_trace("loop-converged")

    missing_eval = store.evaluate_trace("missing")

    assert planner_eval.outcome == "failure"
    assert planner_eval.failure_domain == "planner_or_parser"
    assert json.loads(planner_eval.evidence_json)["first_failure_tool"] == "provider.config"
    assert tool_eval.failure_domain == "capability_failure"
    assert safeguard_eval.outcome == "failure"
    assert safeguard_eval.failure_domain == "safeguard_policy"
    assert "safeguard hook decision" in safeguard_eval.diagnostic
    assert runtime_eval.failure_domain == "runtime"
    assert completion_eval.outcome == "failure"
    assert completion_eval.failure_domain == "checker_blocked"
    assert no_response_eval.outcome == "failure"
    assert no_response_eval.failure_domain == "provider_no_response"
    assert listed_completion_evals[0].id == completion_eval.id
    assert any(evaluation.id == completion_eval.id for evaluation in listed_all_evals)
    completion_evidence = json.loads(completion_eval.evidence_json)
    assert completion_evidence["recovery_plan_recorded"] is True
    assert completion_evidence["recovery_blocked"] is True
    assert completion_evidence["recovery_detail_keys"] == [
        "blocked_entity_type",
        "run_status",
    ]
    assert pending_eval.outcome == "degraded"
    assert pending_eval.failure_domain == "missing_completion_check"
    assert json.loads(pending_eval.evidence_json)["pending_run_completion_risk"] is True
    assert loop_failed_eval.outcome == "failure"
    assert loop_failed_eval.failure_domain == "planner_or_parser"
    assert loop_approval_eval.outcome == "degraded"
    assert loop_approval_eval.failure_domain == "approval_loop"
    assert loop_converged_eval.outcome == "degraded"
    assert loop_converged_eval.failure_domain == "loop_no_progress"
    loop_evidence = json.loads(loop_approval_eval.evidence_json)
    assert loop_evidence["loop_decisions"][0]["failure_domain"] == "approval_loop"
    assert loop_evidence["loop_decisions"][0]["failed_gates"] == ["approval_gate"]
    assert missing_eval.outcome == "unknown"
    assert missing_eval.failure_domain == "trace_missing"
