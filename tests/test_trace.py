from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from navi.api import create_app
from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.capability_contract import CAPABILITY_ERROR_REASON_KEY
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
    assert json.loads(events[0].output_json)["approval_code"] == "123456"
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
    assert runs[2].name == "Decision: finalize"
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


def test_trace_delete_api_endpoints(tmp_path):
    store = TraceStore(tmp_path)
    store.add_event(trace_id="trace-to-delete-1", phase="turn.start")
    store.add_event(trace_id="trace-to-delete-2", phase="turn.start")
    
    app = create_app(tmp_path)
    api_key = (tmp_path / "api_key").read_text(encoding="utf-8").strip()
    client = TestClient(app)

    # Verify initial traces exist
    res = client.get("/v1/traces", headers={"X-API-Key": api_key})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert len(res.json()["data"]["traces"]) == 2

    # Delete one trace
    delete_res = client.delete("/v1/traces/trace-to-delete-1", headers={"X-API-Key": api_key})
    assert delete_res.status_code == 200
    assert delete_res.json()["ok"] is True
    assert delete_res.json()["data"] == {"status": "ok"}

    # Verify only one trace remains
    res = client.get("/v1/traces", headers={"X-API-Key": api_key})
    assert res.json()["ok"] is True
    assert len(res.json()["data"]["traces"]) == 1
    assert res.json()["data"]["traces"][0]["trace_id"] == "trace-to-delete-2"

    # Clear all traces
    clear_res = client.delete("/v1/traces", headers={"X-API-Key": api_key})
    assert clear_res.status_code == 200
    assert clear_res.json()["ok"] is True
    assert clear_res.json()["data"] == {"status": "ok"}

    # Verify no traces remain
    res = client.get("/v1/traces", headers={"X-API-Key": api_key})
    assert res.json()["ok"] is True
    assert len(res.json()["data"]["traces"]) == 0


def test_trace_ui_read_endpoints_are_public_but_writes_require_api_key(tmp_path):
    store = TraceStore(tmp_path)
    store.add_event(trace_id="trace-public", phase="turn.start")
    app = create_app(tmp_path)
    client = TestClient(app)

    list_res = client.get("/v1/traces")
    assert list_res.status_code == 200
    assert list_res.json()["ok"] is True

    detail_res = client.get("/v1/traces/trace-public")
    assert detail_res.status_code == 200
    assert detail_res.json()["ok"] is True

    runs_res = client.get("/v1/traces/trace-public/runs")
    assert runs_res.status_code == 200
    assert runs_res.json()["ok"] is True

    delete_res = client.delete("/v1/traces/trace-public")
    assert delete_res.status_code == 401

    evaluate_res = client.post("/v1/traces/trace-public/evaluate")
    assert evaluate_res.status_code == 401


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

    assert meta_by_id["trace-degraded"]["has_error"] is False
    assert meta_by_id["trace-degraded"]["has_issue"] is True
    assert meta_by_id["trace-degraded"]["outcome"] == "degraded"
    assert meta_by_id["trace-degraded"]["failure_domain"] == "loop_no_progress"
    assert meta_by_id["trace-degraded"]["failed_event_count"] == 1
    assert set(store.list_trace_ids(has_error=True)) == set()
    assert set(store.list_trace_ids(has_error=False)) == {"trace-degraded", "trace-success"}


def test_trace_evaluation_degrades_recovered_capability_failure(tmp_path):
    store = TraceStore(tmp_path)
    store.add_event(
        trace_id="trace-recovered-capability",
        phase="capability.result",
        ok=False,
        tool="shell.run",
        output_data={"facts": {"exit_code": 1}},
        message="find expression failed",
    )
    store.add_loop_decision(
        trace_id="trace-recovered-capability",
        decision=LoopDecision(
            decision=LoopDecisionKind.FINALIZE,
            reason=LoopReason.TERMINAL_RESULT,
            failure_domain=TraceFailureDomain.NONE,
            tool="respond",
        ),
    )

    evaluation = store.evaluate_trace("trace-recovered-capability")
    evidence = json.loads(evaluation.evidence_json)

    assert evaluation.outcome == "degraded"
    assert evaluation.failure_domain == "capability_failure"
    assert evidence["evaluation_rule"] == "capability_failed_then_recovered"
    assert evidence["first_failure_tool"] == "shell.run"
    assert evidence["recovered_after_first_failure"] is True
    assert evidence["recovery_decision"]["decision"] == "finalize"


def test_trace_evaluation_keeps_unrecovered_capability_failure_as_failure(tmp_path):
    store = TraceStore(tmp_path)
    store.add_event(
        trace_id="trace-unrecovered-capability",
        phase="capability.result",
        ok=False,
        tool="shell.run",
        output_data={"facts": {"exit_code": 1}},
    )

    evaluation = store.evaluate_trace("trace-unrecovered-capability")
    evidence = json.loads(evaluation.evidence_json)

    assert evaluation.outcome == "failure"
    assert evaluation.failure_domain == "capability_failure"
    assert evidence["evaluation_rule"] == "capability_failed_event"
    assert "recovered_after_first_failure" not in evidence


def test_trace_evaluation_degrades_recovered_capability_schema_mismatch(tmp_path):
    store = TraceStore(tmp_path)
    store.add_event(
        trace_id="trace-recovered-schema",
        phase="capability.result",
        ok=False,
        tool="memory.recall",
        output_data={"facts": {CAPABILITY_ERROR_REASON_KEY: "schema_mismatch"}},
    )
    store.add_loop_decision(
        trace_id="trace-recovered-schema",
        decision=LoopDecision(
            decision=LoopDecisionKind.FINALIZE,
            reason=LoopReason.COMPLETION_EVIDENCE_TRUE,
            failure_domain=TraceFailureDomain.NONE,
        ),
    )

    evaluation = store.evaluate_trace("trace-recovered-schema")
    evidence = json.loads(evaluation.evidence_json)

    assert evaluation.outcome == "degraded"
    assert evaluation.failure_domain == "planner_or_parser"
    assert evidence["evaluation_rule"] == "capability_input_schema_mismatch_then_recovered"
    assert evidence["first_failure_tool"] == "memory.recall"
    assert evidence["recovered_after_first_failure"] is True


def test_trace_evaluation_degrades_recovered_planner_parse_failure(tmp_path):
    store = TraceStore(tmp_path)
    store.add_event(
        trace_id="trace-recovered-planner",
        phase="planner.parse_error",
        ok=False,
        tool="system.planner_error",
        message="planner JSON parse failed",
    )
    store.add_loop_decision(
        trace_id="trace-recovered-planner",
        decision=LoopDecision(
            decision=LoopDecisionKind.FINALIZE,
            reason=LoopReason.TERMINAL_RESULT,
            failure_domain=TraceFailureDomain.NONE,
        ),
    )

    evaluation = store.evaluate_trace("trace-recovered-planner")
    evidence = json.loads(evaluation.evidence_json)

    assert evaluation.outcome == "degraded"
    assert evaluation.failure_domain == "planner_or_parser"
    assert evidence["evaluation_rule"] == "planner_failed_then_recovered"
    assert evidence["recovered_after_first_failure"] is True
