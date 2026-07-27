from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from navi.api import create_app
from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.capability_contract import CAPABILITY_ERROR_REASON_KEY
from navi.config import load_config
from navi.db import connect
from navi.loop import LoopCheckName, LoopDecisionKind, LoopReason, TraceFailureDomain
from navi.loop_contracts import GoalSpec, LoopNode, LoopSpec, VerificationKind, VerificationStep
from navi.loop_control_service import LoopControlService, OpenGoalRequest
from navi.loop_runs import LoopRunStore
from navi.lifecycle import Acceptance, Phase, Resolution
from navi.runs import RunStore
from navi.trace import LoopCheckResult, TraceStore
from navi.loop import LoopDecision


def _loop_spec_for_trace(goal_id: str) -> LoopSpec:
    return LoopSpec.from_goal(
        GoalSpec(
            objective="show complete trace loop state",
            scope=("repo:/tmp/project",),
            acceptance_criteria=("loop transition is visible in trace UI",),
            permission_ceiling="read",
        ),
        goal_id=goal_id,
        allowed_capabilities=("respond",),
        verification_ladder=(
            VerificationStep(
                kind=VerificationKind.LLM_CHECKER,
                name="objective_check",
                required=False,
                evidence_key="capability_result",
            ),
        ),
    )


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
        "api_key": "[REDACTED]",
        "items": [{"token": "[REDACTED]"}, {"safe": "value"}],
        "nested": {"password": "[REDACTED]"},
        "resume_text": "phone [REDACTED_PHONE] email [REDACTED_EMAIL]",
    }
    assert json.loads(events[0].output_json)["approval_code"] == "[REDACTED]"
    assert "[REDACTED_PHONE]" in json.loads(events[0].output_json)["contact"]
    assert "[REDACTED_EMAIL]" in json.loads(events[0].output_json)["contact"]
    assert events[0].message == "planned for [REDACTED_EMAIL] [REDACTED_PHONE]"
    assert len(decisions) == 1
    assert json.loads(decisions[0].output_json)["evidence"]["api_key"] == "[REDACTED]"
    assert runs[0].id == trace_id
    assert runs[0].run_type == "chain"
    assert runs[0].thread_id == "session-redaction"
    assert runs[0].metadata["event_count"] == 2
    assert runs[1].run_type == "llm"
    assert runs[1].inputs["api_key"] == "[REDACTED]"
    assert runs[2].name == "Decision: finalize"
    assert runs[2].parent_run_id == trace_id
    assert runs[2].feedback == {}


def test_trace_blob_resolution_preserves_non_blob_loop_decision_payloads(tmp_path):
    store = TraceStore(tmp_path)
    trace_id = "trace-mixed-blob"

    store.add_event(
        trace_id=trace_id,
        phase="planner.syscall",
        output_data={"large": "x" * 2048},
    )
    store.add_loop_decision(
        trace_id=trace_id,
        decision=LoopDecision(
            decision=LoopDecisionKind.CONTINUE,
            reason=LoopReason.CAPABILITY_FACT_RECORDED,
            tool="state_graph.side_effect.commit",
            evidence={
                "side_effect": {
                    "state": "released_for_connector_commit",
                    "artifact": "/tmp/outbox/resume.docx",
                }
            },
        ),
    )

    decisions = store.list_loop_decisions(trace_id)

    assert len(decisions) == 1
    payload = json.loads(decisions[0].output_json)
    assert payload["tool"] == "state_graph.side_effect.commit"
    assert payload["evidence"]["side_effect"]["state"] == "released_for_connector_commit"


def test_loop_decision_filter_is_applied_before_pagination(tmp_path):
    store = TraceStore(tmp_path)
    trace_id = "trace-decision-pagination"
    for index in range(6):
        store.add_event(trace_id=trace_id, phase="capability.result", message=str(index))
    store.add_loop_decision(
        trace_id=trace_id,
        decision=LoopDecision(
            decision=LoopDecisionKind.CONTINUE,
            reason=LoopReason.CAPABILITY_FACT_RECORDED,
        ),
    )

    decisions = store.list_loop_decisions(trace_id, limit=1)

    assert len(decisions) == 1
    assert decisions[0].phase == "loop.decision"


def test_trace_store_redacts_legacy_rows_on_schema_migration(tmp_path):
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
        conn.execute(
            """
            INSERT INTO schema_versions(component, version, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(component) DO UPDATE
            SET version = excluded.version, updated_at = excluded.updated_at
            """,
            ("traces", 1, 2_000_000_000.0),
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


def test_trace_store_replaces_legacy_evaluation_index_and_deduplicates(tmp_path):
    TraceStore(tmp_path)
    with connect(tmp_path / "traces.db") as conn:
        conn.execute("DROP INDEX idx_trace_evaluations_trace")
        conn.execute("CREATE INDEX idx_trace_evaluations_trace ON trace_evaluations(trace_id)")
        conn.executemany(
            """
            INSERT INTO trace_evaluations(
                id, trace_id, outcome, failure_domain, evidence_json, created_at
            ) VALUES (?, 'trace-legacy', ?, 'none', '{}', ?)
            """,
            (("old", "success", 1.0), ("latest", "failure", 2.0)),
        )
        conn.execute("UPDATE schema_versions SET version = 2 WHERE component = 'traces'")

    store = TraceStore(tmp_path)

    evaluations = store.list_evaluations("trace-legacy")
    assert [item.id for item in evaluations] == ["latest"]
    replacement = store.record_evaluation(
        trace_id="trace-legacy",
        outcome="success",
        failure_domain="none",
    )
    assert replacement.id == "latest"
    with connect(tmp_path / "traces.db") as conn:
        index = conn.execute("PRAGMA index_list(trace_evaluations)").fetchall()
    assert any(row[1] == "idx_trace_evaluations_trace" and row[2] == 1 for row in index)


def test_trace_store_locked_write_does_not_fail_runtime(tmp_path, monkeypatch):
    store = TraceStore(tmp_path)

    @contextmanager
    def locked_connect(_path):
        raise sqlite3.OperationalError("database is locked")
        yield

    monkeypatch.setattr("navi.trace.connect", locked_connect)

    event = store.add_event(
        trace_id="trace-locked",
        phase="planner.syscall",
        input_data={"text": "ifnodoraemon@example.com"},
    )

    assert event.trace_id == "trace-locked"
    assert event.phase == "planner.syscall"
    assert json.loads(event.input_json) == {"text": "[REDACTED_EMAIL]"}


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


def test_trace_decisions_api_returns_structured_loop_decisions(tmp_path, valid_runtime_config):
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
    api_key = load_config(tmp_path).api.api_key
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


def test_trace_api_includes_durable_loop_run_details_for_web_tree(tmp_path, valid_runtime_config):
    loop_store = LoopRunStore(tmp_path)
    loop_run = loop_store.create_run(_loop_spec_for_trace("goal-trace"))
    checkpoint = loop_store.write_checkpoint(
        loop_run.run_id,
        node=LoopNode.PLAN,
        inputs={"planned_capability": {"tool": "respond"}},
        state=loop_run.to_dict(),
    )
    loop_store.transition(
        loop_run.run_id,
        node=LoopNode.EXECUTE,
        checkpoint_id=checkpoint.id,
        condition="plan_ready",
        evidence={"planned_capability": {"tool": "respond"}},
    )
    store = TraceStore(tmp_path)
    store.add_event(
        trace_id="trace-loop-ui",
        phase="turn.start",
        session_id="session-loop-ui",
        input_data={"message": "show loop"},
    )
    store.add_event(
        trace_id="trace-loop-ui",
        phase="capability.result",
        output_data={"facts": {"loop_run_id": loop_run.run_id}},
    )

    app = create_app(tmp_path)
    api_key = load_config(tmp_path).api.api_key
    client = TestClient(app)

    list_response = client.get("/v1/traces", headers={"X-API-Key": api_key})
    assert list_response.status_code == 200
    meta = list_response.json()["data"]["traces"][0]
    assert meta["thread_id"] == "session-loop-ui"

    trace_response = client.get(
        "/v1/traces/trace-loop-ui",
        headers={"X-API-Key": api_key},
    )
    assert trace_response.status_code == 200
    payload = trace_response.json()["data"]
    assert payload["loop_runs"][0]["run_state"]["run_id"] == loop_run.run_id
    run_names = {item["name"] for item in payload["runs"]}
    assert "LoopRun: execute" in run_names
    assert "Checkpoint: plan" in run_names
    assert "Loop Transition: execute" in run_names
    engine_runs = [item for item in payload["runs"] if item["run_type"] == "engine"]
    assert engine_runs


def test_trace_root_uses_correlated_durable_loop_failure_as_authoritative_status(tmp_path):
    opened = LoopControlService(tmp_path).open_goal(
        OpenGoalRequest(
            objective="report current account usage",
            workspace=str(tmp_path),
            source="weixin",
            peer_id="wx-user",
            sender_id="wx-user",
            allowed_capabilities=("account.usage", "respond"),
            auto_start=False,
            execution_mode="background",
        )
    )
    owner = "trace-test-worker"
    loop_store = LoopRunStore(tmp_path)
    assert loop_store.claim_for_execution(opened.loop_run.run_id, owner=owner) is not None
    loop_store.fail_active_run(
        opened.loop_run.run_id,
        lease_owner=owner,
        evidence={"reason_code": "semantic_check_failed"},
    )
    RunStore(tmp_path).update_run(
        opened.run.id,
        phase=Phase.ENDED,
        acceptance=Acceptance.REJECTED,
        resolution=Resolution.BLOCKED,
        error="loop_blocked",
    )
    trace_store = TraceStore(tmp_path)
    trace_store.add_event(
        trace_id=opened.run.id,
        run_id=opened.run.id,
        phase="agent.role_result",
        model_role="notification",
        ok=True,
        input_data={"facts": {"error": "loop_blocked"}},
        output_data={"notify": False, "message": ""},
    )

    views = trace_store.list_run_views(opened.run.id)
    details = trace_store.list_loop_run_details(opened.run.id)

    assert views[0].id == opened.run.id
    assert views[0].status == "error"
    assert views[0].metadata["authoritative_status_source"] == "durable_loop_run"
    assert any(item.id == f"looprun_{opened.loop_run.run_id}" for item in views)
    assert details[0]["run_state"]["run_id"] == opened.loop_run.run_id


def test_trace_delete_api_endpoints(tmp_path, valid_runtime_config):
    store = TraceStore(tmp_path)
    store.add_event(trace_id="trace-to-delete-1", phase="turn.start")
    store.add_event(trace_id="trace-to-delete-2", phase="turn.start")

    app = create_app(tmp_path)
    api_key = load_config(tmp_path).api.api_key
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
    deletion = delete_res.json()["data"]
    assert deletion["entity_type"] == "trace"
    assert deletion["entity_id"] == "trace-to-delete-1"
    assert deletion["existed"] is True
    assert deletion["verified_after"] == {"event_count": 0, "evaluation_count": 0}

    # Verify only one trace remains
    res = client.get("/v1/traces", headers={"X-API-Key": api_key})
    assert res.json()["ok"] is True
    assert len(res.json()["data"]["traces"]) == 1
    assert res.json()["data"]["traces"][0]["trace_id"] == "trace-to-delete-2"

    # Clear all traces
    clear_res = client.delete("/v1/traces", headers={"X-API-Key": api_key})
    assert clear_res.status_code == 200
    assert clear_res.json()["ok"] is True
    deletion = clear_res.json()["data"]
    assert deletion["entity_type"] == "trace_collection"
    assert deletion["trace_count"] == 1
    assert deletion["verified_after"] == {
        "trace_count": 0,
        "event_count": 0,
        "evaluation_count": 0,
    }

    # Verify no traces remain
    res = client.get("/v1/traces", headers={"X-API-Key": api_key})
    assert res.json()["ok"] is True
    assert len(res.json()["data"]["traces"]) == 0


def test_trace_ui_read_endpoints_are_public_but_writes_require_api_key(
    tmp_path, valid_runtime_config
):
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


@pytest.mark.asyncio
async def test_trace_delete_capability_requires_explicit_scope_and_verifies_deletion(tmp_path):
    store = TraceStore(tmp_path)
    store.add_event(trace_id="trace-delete", phase="turn.start")
    registry = build_capability_registry(tmp_path, project_dir=tmp_path, execution_context="api")

    invalid = await registry.invoke(
        "trace.delete",
        {},
        permission="write",
        context=CapabilityContext(home=tmp_path, workspace=str(tmp_path)),
    )
    result = await registry.invoke(
        "trace.delete",
        {"trace_id": "trace-delete"},
        permission="write",
        context=CapabilityContext(home=tmp_path, workspace=str(tmp_path)),
    )

    assert invalid.ok is False
    assert invalid.error_reason == "schema_mismatch"
    assert result.ok is True
    assert result.facts is not None
    assert result.facts["verified_after"] == {"event_count": 0, "evaluation_count": 0}
    assert store.list_events("trace-delete") == []


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


def test_trace_converged_without_failed_facts_is_success(tmp_path):
    store = TraceStore(tmp_path)
    store.add_event(trace_id="trace-converged", phase="turn.start", ok=True)
    store.add_loop_decision(
        trace_id="trace-converged",
        decision=LoopDecision(
            decision=LoopDecisionKind.CONVERGED,
            reason=LoopReason.COMPLETION_EVIDENCE_TRUE,
            failure_domain=TraceFailureDomain.NONE,
        ),
    )

    evaluation = store.evaluate_trace("trace-converged")

    assert evaluation.outcome == "success"
    assert evaluation.failure_domain == "none"
    assert json.loads(evaluation.evidence_json)["evaluation_rule"] == ("loop_decision_converged")


def test_trace_waiting_approval_is_successful_pause_not_failure(tmp_path):
    store = TraceStore(tmp_path)
    store.add_event(
        trace_id="trace-waiting-approval",
        phase="capability.result",
        tool="shell.run",
        ok=False,
        output_data={
            "facts": {
                "entity_type": "approval_request",
                "approval": {"id": "approval-1"},
            }
        },
    )
    store.add_loop_decision(
        trace_id="trace-waiting-approval",
        decision=LoopDecision(
            decision=LoopDecisionKind.BLOCKED,
            reason=LoopReason.APPROVAL_REQUIRED,
            failure_domain=TraceFailureDomain.SAFEGUARD_POLICY,
        ),
    )

    evaluation = store.evaluate_trace("trace-waiting-approval")
    evidence = json.loads(evaluation.evidence_json)

    assert evaluation.outcome == "success"
    assert evaluation.failure_domain == "none"
    assert evidence["evaluation_rule"] == "loop_decision_waiting_approval"
    assert evidence["pending_external_gate"] is True
    assert evidence["completion_evidence"] is False


def test_trace_external_pause_is_successful_pause_not_approval(tmp_path):
    store = TraceStore(tmp_path)
    store.add_event(
        trace_id="trace-external-pause",
        phase="capability.result",
        tool="channel.send_file",
        ok=True,
        output_data={
            "action": "connector_outbound",
            "facts": {
                "connector_delivery": {
                    "mode": "durable",
                    "path": "/tmp/report.xlsx",
                }
            },
        },
    )
    decision_event = store.add_loop_decision(
        trace_id="trace-external-pause",
        decision=LoopDecision(
            decision=LoopDecisionKind.BLOCKED,
            reason=LoopReason.EXTERNAL_PAUSE,
            failure_domain=TraceFailureDomain.NONE,
            tool="connector_outbound",
            gate_results=(
                LoopCheckResult(
                    name=LoopCheckName.EXTERNAL_PAUSE,
                    passed=True,
                    reason=str(LoopReason.EXTERNAL_PAUSE),
                ),
            ),
        ),
    )

    evaluation = store.evaluate_trace("trace-external-pause")
    evidence = json.loads(evaluation.evidence_json)

    assert decision_event.ok is True
    assert evaluation.outcome == "success"
    assert evaluation.failure_domain == "none"
    assert evidence["evaluation_rule"] == "loop_decision_external_pause"
    assert evidence["pending_external_action"] is True
    assert "pending_external_gate" not in evidence


def test_trace_ordinary_ask_recorded_as_approval_gate_is_degraded(tmp_path):
    store = TraceStore(tmp_path)
    store.add_event(
        trace_id="trace-ordinary-ask",
        phase="capability.result",
        tool="ask.user",
        ok=True,
        output_data={
            "action": "ask",
            "facts": {"options": ["one"]},
        },
    )
    store.add_loop_decision(
        trace_id="trace-ordinary-ask",
        decision=LoopDecision(
            decision=LoopDecisionKind.BLOCKED,
            reason=LoopReason.APPROVAL_REQUIRED,
            failure_domain=TraceFailureDomain.SAFEGUARD_POLICY,
        ),
    )

    evaluation = store.evaluate_trace("trace-ordinary-ask")
    evidence = json.loads(evaluation.evidence_json)

    assert evaluation.outcome == "degraded"
    assert evaluation.failure_domain == "safeguard_policy"
    assert evidence["evaluation_rule"] == "ordinary_ask_recorded_as_approval_gate"
    assert evidence["ordinary_ask_recorded_as_approval_gate"] is True


def test_trace_duplicate_entity_mutation_is_degraded(tmp_path):
    store = TraceStore(tmp_path)
    for _ in range(2):
        store.add_event(
            trace_id="trace-duplicate-cancel",
            phase="capability.result",
            tool="goal.cancel",
            ok=True,
            output_data={
                "facts": {
                    "entity_type": "goal",
                    "entity_id": "goal-1",
                    "goal_id": "goal-1",
                    "state_transition": "cancelled",
                }
            },
        )
    store.add_loop_decision(
        trace_id="trace-duplicate-cancel",
        decision=LoopDecision(
            decision=LoopDecisionKind.CONVERGED,
            reason=LoopReason.COMPLETION_EVIDENCE_TRUE,
            failure_domain=TraceFailureDomain.NONE,
        ),
    )

    evaluation = store.evaluate_trace("trace-duplicate-cancel")
    evidence = json.loads(evaluation.evidence_json)

    assert evaluation.outcome == "degraded"
    assert evaluation.failure_domain == "loop_no_progress"
    assert evidence["evaluation_rule"] == "duplicate_entity_mutation"
    assert evidence["duplicate_mutation"]["refs"] == {"goal:goal-1:cancelled": 2}


def test_trace_duplicate_collection_entity_mutation_is_degraded(tmp_path):
    store = TraceStore(tmp_path)
    for _ in range(2):
        store.add_event(
            trace_id="trace-duplicate-batch-cancel",
            phase="capability.result",
            tool="goal.cancel",
            ok=True,
            output_data={
                "facts": {
                    "entity_type": "goal_collection",
                    "state_transition": "batch_cancelled",
                    "cancelled_goals": [
                        {
                            "goal_id": "goal-1",
                            "state_transition": "already_terminal",
                        }
                    ],
                }
            },
        )
    store.add_loop_decision(
        trace_id="trace-duplicate-batch-cancel",
        decision=LoopDecision(
            decision=LoopDecisionKind.FAILED,
            reason=LoopReason.COMPLETION_CHECKER_BLOCKED,
            failure_domain=TraceFailureDomain.CHECKER_BLOCKED,
        ),
    )

    evaluation = store.evaluate_trace("trace-duplicate-batch-cancel")
    evidence = json.loads(evaluation.evidence_json)

    assert evaluation.outcome == "degraded"
    assert evaluation.failure_domain == "loop_no_progress"
    assert evidence["evaluation_rule"] == "duplicate_entity_mutation"
    assert evidence["duplicate_mutation"]["refs"] == {"cancelled_goals:goal-1:already_terminal": 2}


def test_trace_final_text_keywords_do_not_drive_evaluation(tmp_path):
    store = TraceStore(tmp_path)
    store.add_event(
        trace_id="trace-final-keywords",
        phase="capability.result",
        tool="goal.state",
        ok=True,
        output_data={"facts": {"view": "current", "current_goals": []}},
    )
    store.add_event(
        trace_id="trace-final-keywords",
        phase="turn.final",
        ok=True,
        message="当前共有 18 个任务，其中后台计划任务 3 个。",
    )
    store.add_loop_decision(
        trace_id="trace-final-keywords",
        decision=LoopDecision(
            decision=LoopDecisionKind.CONVERGED,
            reason=LoopReason.COMPLETION_EVIDENCE_TRUE,
            failure_domain=TraceFailureDomain.NONE,
        ),
    )

    evaluation = store.evaluate_trace("trace-final-keywords")
    evidence = json.loads(evaluation.evidence_json)

    assert evaluation.outcome == "success"
    assert evaluation.failure_domain == "none"
    assert evidence["evaluation_rule"] == "loop_decision_converged"


def test_trace_converged_after_capability_failure_is_degraded(tmp_path):
    store = TraceStore(tmp_path)
    store.add_event(
        trace_id="trace-recovered-converged",
        phase="capability.result",
        tool="shell.run",
        ok=False,
        output_data={"facts": {"exit_code": 1}},
    )
    store.add_loop_decision(
        trace_id="trace-recovered-converged",
        decision=LoopDecision(
            decision=LoopDecisionKind.CONVERGED,
            reason=LoopReason.COMPLETION_EVIDENCE_TRUE,
            failure_domain=TraceFailureDomain.NONE,
        ),
    )

    evaluation = store.evaluate_trace("trace-recovered-converged")

    assert evaluation.outcome == "degraded"
    assert evaluation.failure_domain == "capability_failure"
    assert json.loads(evaluation.evidence_json)["evaluation_rule"] == (
        "capability_failed_then_recovered"
    )


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
