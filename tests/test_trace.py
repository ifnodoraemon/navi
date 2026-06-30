from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from navi.api import create_app
from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
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
            "resume_text": "phone 15709610082 email ifnodoraemon@example.com",
        },
        output_data={
            "approval_code": "123456",
            "safe": "ok",
            "contact": "15709610082 ifnodoraemon@example.com",
        },
        message="planned for ifnodoraemon@example.com 15709610082",
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
        "resume_text": "phone [REDACTED_PHONE] email [REDACTED_EMAIL]",
    }
    assert json.loads(events[0].output_json)["approval_code"] == "[redacted]"
    assert "[REDACTED_PHONE]" in json.loads(events[0].output_json)["contact"]
    assert "[REDACTED_EMAIL]" in json.loads(events[0].output_json)["contact"]
    assert events[0].message == "planned for [REDACTED_EMAIL] [REDACTED_PHONE]"
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


def test_trace_store_redacts_legacy_rows_on_init(tmp_path):
    TraceStore(tmp_path)
    with connect(tmp_path / "traces.db") as conn:
        conn.execute(
            """
            INSERT INTO trace_blobs(hash, content) VALUES (?, ?)
            """,
            (
                "legacy-hash",
                "简历 电话：15709610082 邮箱：ifnodoraemon@example.com",
            ),
        )
        conn.execute(
            """
            INSERT INTO trace_events(
                id, trace_id, session_id, run_id, phase, source, peer_id,
                sender_id, tool, model_role, ok, input_json, output_json,
                message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "event-legacy",
                "trace-legacy",
                "",
                "",
                "capability.result",
                "",
                "",
                "",
                "file.read",
                "planner",
                1,
                json.dumps({"query": "ifnodoraemon@example.com"}),
                json.dumps({"content": {"$blob": "legacy-hash"}}),
                "已读取 15709610082 ifnodoraemon@example.com",
                2_000_000_000.0,
            ),
        )
        conn.execute(
            """
            INSERT INTO trace_evaluations(
                id, trace_id, outcome, failure_domain, evidence_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "eval-legacy",
                "trace-legacy",
                "success",
                "none",
                json.dumps({"contact": "15709610082 ifnodoraemon@example.com"}),
                2_000_000_000.0,
            ),
        )

    store = TraceStore(tmp_path)
    event = store.list_events("trace-legacy")[0]
    evaluation = store.list_evaluations("trace-legacy")[0]

    assert "15709610082" not in event.input_json
    assert "ifnodoraemon@example.com" not in event.input_json
    assert "15709610082" not in event.output_json
    assert "ifnodoraemon@example.com" not in event.output_json
    assert event.message == "已读取 [REDACTED_PHONE] [REDACTED_EMAIL]"
    assert "15709610082" not in evaluation.evidence_json
    assert "ifnodoraemon@example.com" not in evaluation.evidence_json
    with connect(tmp_path / "traces.db") as conn:
        blob_content = conn.execute(
            "SELECT content FROM trace_blobs WHERE hash = ?", ("legacy-hash",)
        ).fetchone()[0]
    assert blob_content == "简历 电话：[REDACTED_PHONE] 邮箱：[REDACTED_EMAIL]"


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
                    reason=LoopReason.TERMINAL_RESULT,
                    evidence={"terminal_action": "chat"},
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


@pytest.mark.asyncio
async def test_trace_evaluate_capability_returns_structured_evidence(tmp_path):
    store = TraceStore(tmp_path)
    store.add_event(trace_id="trace-ok", phase="turn.start")
    registry = build_capability_registry(tmp_path, project_dir=tmp_path, execution_context="api")

    result = await registry.invoke(
        "trace.evaluate",
        {"trace_id": "trace-ok"},
        permission="write",
        context=CapabilityContext(home=tmp_path, workspace=str(tmp_path)),
    )

    assert result.ok
    assert result.facts is not None
    evaluation = result.facts["evaluation"]
    assert "diagnostic" not in evaluation
    assert "evidence_json" not in evaluation
    assert evaluation["evidence"]["evaluation_rule"] == "no_failed_or_degraded_rule"


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
        trace_id="capability-input-schema",
        phase="capability.result",
        ok=False,
        tool="final.answer",
        output_data={
            "facts": {
                "error_reason": "schema_mismatch",
                "schema_errors": ["$.message is required"],
                "tool": "final.answer",
            }
        },
        message="capability final.answer input schema mismatch",
    )
    input_schema_eval = store.evaluate_trace("capability-input-schema")

    store.add_event(
        trace_id="capability-output-schema",
        phase="capability.result",
        ok=False,
        tool="delegate.run",
        output_data={
            "facts": {
                "error_reason": "schema_mismatch",
                "schema_errors": ["$.run_id is required"],
                "result_action": "delegation_created",
                "tool": "delegate.run",
            }
        },
        message="capability delegate.run output schema mismatch",
    )
    output_schema_eval = store.evaluate_trace("capability-output-schema")

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
        trace_id="approval-pause",
        phase="capability.result",
        ok=False,
        tool="shell.run",
        output_data={
            "facts": {
                "approval": {"action": "execute:shell.run", "code": "123456"},
                "entity_id": "approval-1",
                "entity_type": "approval_request",
                "reason": "sensitive_op_requires_approval",
                "run_id": "run-approval",
                "state_transition": "created",
            }
        },
        message="approval required",
    )
    store.add_loop_decision(
        trace_id="approval-pause",
        decision=LoopDecision(
            decision=LoopDecisionKind.PAUSE_FOR_APPROVAL,
            reason=LoopReason.APPROVAL_REQUIRED,
            failure_domain=TraceFailureDomain.NONE,
            gate_results=(
                LoopCheckResult(
                    name=LoopCheckName.APPROVAL_GATE,
                    passed=True,
                    severity="info",
                    reason=LoopReason.APPROVAL_REQUIRED,
                ),
            ),
        ),
    )
    store.add_event(trace_id="approval-pause", phase="turn.final", ok=True)
    approval_pause_eval = store.evaluate_trace("approval-pause")
    approval_pause_runs = store.list_run_views("approval-pause")

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

    store.add_event(
        trace_id="pending-resolved",
        phase="capability.result",
        ok=True,
        tool="delegate.status",
        output_data={
            "facts": {"entity_type": "delegation_run", "run_id": "task-2", "status": "pending"}
        },
    )
    store.add_event(
        trace_id="pending-resolved",
        phase="capability.result",
        ok=True,
        tool="delegate.status",
        output_data={
            "facts": {"entity_type": "delegation_run", "run_id": "task-2", "status": "completed"}
        },
    )
    store.add_event(
        trace_id="pending-resolved",
        phase="turn.final",
        ok=True,
        message="done",
    )
    pending_resolved_eval = store.evaluate_trace("pending-resolved")

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

    store.add_event(
        trace_id="loop-input-schema",
        phase="capability.result",
        ok=False,
        tool="file.read",
        output_data={
            "facts": {
                "error_reason": "schema_mismatch",
                "schema_errors": ["$.path is required"],
                "tool": "file.read",
            }
        },
        message="capability file.read input schema mismatch",
    )
    store.add_loop_decision(
        trace_id="loop-input-schema",
        decision=LoopDecision(
            decision="failed",
            reason="capability_failure",
            failure_domain=TraceFailureDomain.CAPABILITY_FAILURE,
            checker_results=(
                LoopCheckResult(
                    name="capability_result",
                    passed=False,
                    severity="error",
                    reason="capability file.read input schema mismatch",
                ),
            ),
        ),
    )
    loop_input_schema_eval = store.evaluate_trace("loop-input-schema")

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
                    reason=LoopReason.APPROVAL_ALREADY_PENDING,
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
                    reason=LoopReason.REPEATED_PROGRESS_SIGNATURE,
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
    assert input_schema_eval.failure_domain == "planner_or_parser"
    assert output_schema_eval.failure_domain == "capability_failure"
    assert safeguard_eval.outcome == "failure"
    assert safeguard_eval.failure_domain == "safeguard_policy"
    assert json.loads(safeguard_eval.evidence_json)["evaluation_rule"] == "safeguard_hook_decision"
    assert runtime_eval.failure_domain == "runtime"
    assert approval_pause_eval.outcome == "success"
    assert approval_pause_eval.failure_domain == "none"
    assert (
        json.loads(approval_pause_eval.evidence_json)["evaluation_rule"]
        == "approval_pause_recorded"
    )
    assert approval_pause_runs[0].status == "success"
    assert approval_pause_runs[1].status == "blocked"
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
    assert pending_resolved_eval.outcome == "success"
    assert pending_resolved_eval.failure_domain == "none"
    assert (
        json.loads(pending_resolved_eval.evidence_json)["evaluation_rule"]
        == "no_failed_or_degraded_rule"
    )
    assert loop_failed_eval.outcome == "failure"
    assert loop_failed_eval.failure_domain == "planner_or_parser"
    assert loop_input_schema_eval.failure_domain == "planner_or_parser"
    assert (
        json.loads(loop_input_schema_eval.evidence_json)["evaluation_rule"]
        == "loop_failure_domain_corrected_by_input_schema"
    )
    assert (
        json.loads(loop_input_schema_eval.evidence_json)["failure_domain_corrected_from"]
        == "capability_failure"
    )
    assert loop_approval_eval.outcome == "degraded"
    assert loop_approval_eval.failure_domain == "approval_loop"
    assert loop_converged_eval.outcome == "degraded"
    assert loop_converged_eval.failure_domain == "loop_no_progress"
    loop_evidence = json.loads(loop_approval_eval.evidence_json)
    assert loop_evidence["loop_decisions"][0]["failure_domain"] == "approval_loop"
    assert loop_evidence["loop_decisions"][0]["failed_gates"] == ["approval_gate"]
    assert missing_eval.outcome == "unknown"
    assert missing_eval.failure_domain == "trace_missing"


def test_trace_meta_uses_evaluation_outcome_not_final_event_status(tmp_path):
    store = TraceStore(tmp_path)
    store.add_event(
        trace_id="trace-degraded",
        phase="capability.result",
        ok=False,
        tool="web.search",
        message="search provider failed",
    )
    store.add_loop_decision(
        trace_id="trace-degraded",
        decision=LoopDecision(
            decision=LoopDecisionKind.CONVERGED,
            reason=LoopReason.REPEATED_PROGRESS_SIGNATURE,
            failure_domain=TraceFailureDomain.LOOP_NO_PROGRESS,
            gate_results=(
                LoopCheckResult(
                    name=LoopCheckName.NO_PROGRESS_GATE,
                    passed=False,
                    severity="warning",
                    reason=LoopReason.REPEATED_PROGRESS_SIGNATURE,
                ),
            ),
        ),
    )
    store.add_event(trace_id="trace-degraded", phase="turn.final", ok=True)
    store.evaluate_trace("trace-degraded")
    store.add_event(trace_id="trace-success", phase="turn.start", ok=True)
    store.add_event(trace_id="trace-success", phase="turn.final", ok=True)
    store.evaluate_trace("trace-success")

    meta_by_id = {item["trace_id"]: item for item in store.list_trace_meta()}

    assert meta_by_id["trace-degraded"]["has_error"] is True
    assert meta_by_id["trace-degraded"]["outcome"] == "degraded"
    assert meta_by_id["trace-degraded"]["failure_domain"] == "loop_no_progress"
    assert meta_by_id["trace-degraded"]["failed_event_count"] == 1
    assert set(store.list_trace_ids(has_error=True)) == {"trace-degraded"}
    assert set(store.list_trace_ids(has_error=False)) == {"trace-success"}
