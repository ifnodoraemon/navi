"""Tests for Product Acceptance runner, scenarios, checks, and reports."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from navi.acceptance import (
    AcceptanceReport,
    AcceptanceScenario,
    _acceptance_checks,
    _acceptance_failure_reason,
    _acceptance_workspace,
    _ensure_list,
    _error_on_failure,
    _file_contains_check,
    _loop_protocol,
    _mismatch_error,
    _reconcile_terminal_resolution,
    _report,
    _report_marker,
    _run_id_from_turn,
    _safe_str_attr,
    _state_snapshot,
    _text_if_exists,
    _turn_facts,
    load_acceptance_scenario,
    report_to_text,
)
from navi.control_plane import AgentTurnResult
from navi.goals import GoalStore
from navi.lifecycle import Phase, Resolution
from navi.runs import RunStore


def test_load_acceptance_scenario(tmp_path: Path) -> None:
    scenario_file = tmp_path / "valid.yaml"
    scenario_file.write_text(
        """
id: test_scenario_1
request: Build a python calculator
approval_template: "approve {{approval_code}}"
source: weixin
peer_id: user123
sender_id: user123
workspace: /tmp/test_ws
expected:
  file_contains:
    path: calc.py
    text: def add
""",
        encoding="utf-8",
    )

    scenario = load_acceptance_scenario(scenario_file)
    assert scenario.id == "test_scenario_1"
    assert scenario.request == "Build a python calculator"
    assert scenario.source == "weixin"
    assert scenario.workspace == "/tmp/test_ws"
    assert scenario.expected["file_contains"]["path"] == "calc.py"


def test_load_acceptance_scenario_invalid(tmp_path: Path) -> None:
    # 1. Non-mapping yaml
    non_map = tmp_path / "list.yaml"
    non_map.write_text("- item1\n- item2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_acceptance_scenario(non_map)

    # 2. Missing request
    no_req = tmp_path / "no_req.yaml"
    no_req.write_text("id: only_id\n", encoding="utf-8")
    with pytest.raises(ValueError, match="requires request"):
        load_acceptance_scenario(no_req)


def test_acceptance_helpers_and_markers(tmp_path: Path) -> None:
    assert _safe_str_attr(None, "phase") == ""
    assert _ensure_list([1, 2]) == [1, 2]
    assert _ensure_list("not-a-list") == []

    assert _report_marker(True, "failed") == "accepted"
    assert _report_marker(False, "failed") == "failed"

    assert _reconcile_terminal_resolution("converged", Resolution.SUCCESS) == Resolution.SUCCESS
    assert _reconcile_terminal_resolution("blocked", Resolution.SUCCESS) == Resolution.SUCCESS
    assert _reconcile_terminal_resolution("converged", Resolution.FAILED) == Resolution.FAILED

    test_file = tmp_path / "sample.txt"
    assert _text_if_exists(test_file) == ""
    test_file.write_text("hello world", encoding="utf-8")
    assert _text_if_exists(test_file) == "hello world"

    assert _error_on_failure(True, "boom") == ""
    assert _error_on_failure(False, "boom") == "boom"
    assert _mismatch_error(True, "err") == ""
    assert _mismatch_error(False, "err") == "err"


def test_report_to_text() -> None:
    report_accepted = AcceptanceReport(
        id="scen_1",
        accepted=True,
        outcome="accepted",
        reason="ok",
        workspace="/tmp/ws",
        run_id="run_1",
        run_phase="ended",
        run_resolution="success",
        goal_phase="ended",
        goal_resolution="success",
    )
    text = report_to_text(report_accepted)
    assert "product_acceptance=accepted" in text
    assert "scenario=scen_1" in text
    assert "run=run_1" in text

    report_failed = AcceptanceReport(
        id="scen_2",
        accepted=False,
        outcome="failed",
        reason="check failed",
        workspace="/tmp/ws",
        errors=["err1", "err2"],
        checks=[{"name": "chk1", "ok": False, "error": "failed"}],
    )
    text_failed = report_to_text(report_failed)
    assert "product_acceptance=failed" in text_failed
    assert "errors:" in text_failed
    assert "- err1" in text_failed
    assert "failed_checks:" in text_failed


def test_acceptance_workspace(tmp_path: Path) -> None:
    # 1. Custom workspace in scenario
    scen_ws = AcceptanceScenario(id="s1", request="req", workspace=str(tmp_path / "custom"))
    assert _acceptance_workspace(None, scen_ws) == (tmp_path / "custom").resolve()

    # 2. Project dir supplied
    scen_no_ws = AcceptanceScenario(id="s2", request="req")
    assert _acceptance_workspace(tmp_path / "proj", scen_no_ws) == (tmp_path / "proj").resolve()

    # 3. Default tempdir root
    ws_default = _acceptance_workspace(None, scen_no_ws)
    assert "navi-product-acceptance" in str(ws_default)


def test_file_contains_check(tmp_path: Path) -> None:
    # 1. Empty path
    res_empty = _file_contains_check({}, workspace=tmp_path)
    assert not res_empty["ok"]
    assert "empty" in res_empty["error"]

    # 2. Path escaping root
    res_escape = _file_contains_check({"path": "../../etc/passwd"}, workspace=tmp_path)
    assert not res_escape["ok"]
    assert "outside workspace" in res_escape["error"]

    # 3. File not found
    res_missing = _file_contains_check({"path": "not_here.txt"}, workspace=tmp_path)
    assert not res_missing["ok"]
    assert "not found" in res_missing["error"]

    # 4. Success match
    f = tmp_path / "test.py"
    f.write_text("print('hello')", encoding="utf-8")
    res_ok = _file_contains_check({"path": "test.py", "text": "hello"}, workspace=tmp_path)
    assert res_ok["ok"]

    # 5. Mismatch
    res_mismatch = _file_contains_check({"path": "test.py", "text": "missing_token"}, workspace=tmp_path)
    assert not res_mismatch["ok"]
    assert "not found" in res_mismatch["error"]


def test_loop_protocol_and_reconcile() -> None:
    # None inputs
    assert _loop_protocol(run=None, goal=None) == {}

    class MockRun:
        resolution = Resolution.SUCCESS

    class MockGoal:
        evidence_json = json.dumps(
            {
                "loop_terminal_state": "converged",
                "checker_report": {"accepted": True},
            }
        )

    proto = _loop_protocol(run=MockRun(), goal=MockGoal())
    assert proto["terminal_state"] == "converged"
    assert proto["completion"]["status"] == Resolution.SUCCESS
    assert proto["verification"]["status"] == "verified"


def test_state_snapshot_and_turn_facts(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    run = runs.create(
        title="test run",
        workspace=str(tmp_path),
        source="cli",
        peer_id="p1",
        sender_id="u1",
    )

    snap = _state_snapshot(runs, run.id)
    assert snap["run_phase"] != ""
    assert "approval_statuses" in snap

    turn = AgentTurnResult(
        action="respond",
        run_id="run_snap_1",
        trace_id="tr_1",
        terminal=True,
        text="completed response",
        facts={"run_id": "run_snap_1"},
    )
    facts = _turn_facts("request", turn)
    assert facts["action"] == "respond"
    assert facts["run_id"] == "run_snap_1"
    assert _run_id_from_turn(turn) == "run_snap_1"


def test_acceptance_checks_evaluation(tmp_path: Path) -> None:
    class MockRun:
        phase = Phase.ENDED
        resolution = Resolution.SUCCESS
        governance = "autonomous"
        result_summary = "All steps completed successfully."

    context = {
        "run": MockRun(),
        "goal_phase": Phase.ENDED,
        "goal_resolution": Resolution.SUCCESS,
        "protocol": {
            "completion": {"status": Resolution.SUCCESS},
            "verification": {"status": "verified"},
        },
    }

    f = tmp_path / "out.txt"
    f.write_text("result = 42", encoding="utf-8")

    expected = {
        "file_contains": {"path": "out.txt", "text": "42"},
        "files": [{"path": "out.txt", "text": "result"}],
    }

    checks = _acceptance_checks(
        expected,
        workspace=tmp_path,
        run=MockRun(),
        goal_phase=Phase.ENDED,
        goal_resolution=Resolution.SUCCESS,
        protocol=context["protocol"],
    )

    assert len(checks) >= 6
    for c in checks:
        assert c["ok"], f"Check failed: {c}"


def test_acceptance_failure_reason() -> None:
    reason = _acceptance_failure_reason(
        errors=["custom error occurred"],
        checks=[],
        run_phase="ended",
        run_resolution="failed",
        goal_phase="ended",
        goal_resolution="success",
        protocol_completion="failed",
        protocol_verification="unverified",
    )
    assert "custom error occurred" in reason


@pytest.mark.asyncio
async def test_run_product_acceptance_no_run_produced(tmp_path: Path):
    from unittest.mock import MagicMock, patch
    from navi.acceptance import run_product_acceptance

    scenario = AcceptanceScenario(
        id="sc-no-run",
        request="do something",
        approval_template="approve {{approval_code}}",
        source="cli",
        peer_id="u1",
        sender_id="u1",
        workspace=str(tmp_path / "ws"),
        expected={},
    )

    with patch("navi.acceptance.build_runtime", return_value=MagicMock()), patch(
        "navi.control_plane.TurnController.handle"
    ) as mock_handle:
        mock_handle.return_value = AgentTurnResult(
            action="respond",
            run_id="",
            trace_id="t1",
            terminal=True,
            text="no run delegated",
        )
        report = await run_product_acceptance(
            home=tmp_path,
            project_dir=tmp_path / "ws",
            scenario=scenario,
        )
        assert report.accepted is False
        assert report.outcome == "failed"
        assert "no run_id" in report.errors[0]


@pytest.mark.asyncio
async def test_run_product_acceptance_terminal_success(tmp_path: Path):
    from unittest.mock import MagicMock, patch
    from navi.acceptance import run_product_acceptance
    from navi.runs import RunStore
    from navi.goals import GoalStore

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")

    runs = RunStore(tmp_path)
    run = runs.create(
        title="calculator",
        workspace=str(ws),
        source="cli",
        peer_id="u1",
        sender_id="u1",
    )
    runs.update_run(
        run.id,
        phase=Phase.ENDED,
        resolution=Resolution.SUCCESS,
        result_summary="Calculated add successfully",
    )

    goals = GoalStore(tmp_path)
    goal = goals.create(objective="calc", workspace=str(ws), run_id=run.id)
    goals.update_state(
        goal.id,
        phase=Phase.ENDED,
        resolution=Resolution.SUCCESS,
        evidence={"loop_terminal_state": "converged", "checker_report": {"accepted": True}},
    )

    scenario = AcceptanceScenario(
        id="sc-success",
        request="create calc",
        approval_template="approve {{approval_code}}",
        source="cli",
        peer_id="u1",
        sender_id="u1",
        workspace=str(ws),
        expected={
            "file_contains": {"path": "calc.py", "text": "def add"},
        },
    )

    with patch("navi.acceptance.build_runtime", return_value=MagicMock()), patch(
        "navi.control_plane.TurnController.handle"
    ) as mock_handle:
        mock_handle.return_value = AgentTurnResult(
            action="respond",
            run_id=run.id,
            trace_id="t1",
            terminal=True,
            text="done",
        )
        report = await run_product_acceptance(
            home=tmp_path,
            project_dir=ws,
            scenario=scenario,
        )
        assert report.accepted is True
        assert report.outcome == "accepted"


@pytest.mark.asyncio
async def test_run_product_acceptance_run_disappeared_and_no_progress(tmp_path: Path):
    from unittest.mock import MagicMock, patch
    from navi.acceptance import run_product_acceptance

    ws = tmp_path / "ws"
    ws.mkdir()

    scenario = AcceptanceScenario(
        id="sc-disappeared",
        request="create calc",
        approval_template="approve {{approval_code}}",
        source="cli",
        peer_id="u1",
        sender_id="u1",
        workspace=str(ws),
        expected={},
    )

    with patch("navi.acceptance.build_runtime", return_value=MagicMock()), patch(
        "navi.control_plane.TurnController.handle"
    ) as mock_handle:
        mock_handle.return_value = AgentTurnResult(
            action="respond",
            run_id="nonexistent-run-id",
            trace_id="t1",
            terminal=True,
            text="done",
        )
        report = await run_product_acceptance(
            home=tmp_path,
            project_dir=ws,
            scenario=scenario,
        )
        assert report.accepted is False
        assert "run disappeared" in report.errors


@pytest.mark.asyncio
async def test_run_product_acceptance_approval_and_queue_actions(tmp_path: Path):
    from unittest.mock import MagicMock, patch
    from navi.acceptance import run_product_acceptance
    from navi.runs import RunStore
    from navi.lifecycle import Governance, Phase

    ws = tmp_path / "ws"
    ws.mkdir()

    runs = RunStore(tmp_path)
    run = runs.create(
        title="approval test",
        workspace=str(ws),
        source="cli",
        peer_id="u1",
        sender_id="u1",
    )
    runs.update_run(run.id, phase=Phase.RUNNING, governance=Governance.AWAITING_APPROVAL)

    scenario = AcceptanceScenario(
        id="sc-appr",
        request="test approval",
        approval_template="approve {{approval_code}}",
        source="cli",
        peer_id="u1",
        sender_id="u1",
        workspace=str(ws),
        expected={},
    )

    with patch("navi.acceptance.build_runtime", return_value=MagicMock()), patch(
        "navi.control_plane.TurnController.handle"
    ) as mock_handle:
        mock_handle.return_value = AgentTurnResult(
            action="respond",
            run_id=run.id,
            trace_id="t1",
            terminal=False,
            text="awaiting approval",
        )
        report_no_auto = await run_product_acceptance(
            home=tmp_path,
            project_dir=ws,
            scenario=scenario,
            auto_approve=False,
        )
        assert report_no_auto.accepted is False
        assert "approval required" in report_no_auto.errors

        report_no_pend = await run_product_acceptance(
            home=tmp_path,
            project_dir=ws,
            scenario=scenario,
            auto_approve=True,
        )
        assert report_no_pend.accepted is False
        assert "run is awaiting approval but no pending approval exists" in report_no_pend.errors



