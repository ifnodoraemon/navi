from __future__ import annotations

from pathlib import Path

import pytest

from navi.evals import (
    load_claw_eval_dataset,
    load_daily_journey_eval_dataset,
    load_delegation_eval_cases,
    load_delegation_eval_dataset,
    load_weixin_journey_eval_dataset,
    match_delegation_eval_case,
    run_claw_eval_dataset,
    delegation_eval_tools,
    run_daily_journey_eval_dataset,
    run_delegation_eval_dataset,
    run_weixin_journey_eval_dataset,
    validate_delegation_eval_dataset,
)
from navi.syscalls import ModelSyscall


def _dataset() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "delegation_cases.yaml"


def _daily_dataset() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "daily_journeys.yaml"


def _claw_dataset() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "claw_navi.yaml"


def _weixin_dataset() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "weixin_journeys.yaml"


def test_task_eval_dataset_matches_capability_manifest(tmp_path):
    dataset = load_delegation_eval_dataset(_dataset())
    errors = validate_delegation_eval_dataset(dataset, delegation_eval_tools(tmp_path, project_dir=tmp_path))

    assert errors == []


def test_task_eval_dataset_has_100_percent_required_scenario_coverage():
    dataset = load_delegation_eval_dataset(_dataset())
    required = set(dataset["coverage"]["required_categories"])
    observed = {str(case["category"]) for case in dataset["cases"]}

    assert required <= observed
    assert len(required) >= 18


def test_task_eval_dataset_has_100_percent_required_tool_coverage(tmp_path):
    dataset = load_delegation_eval_dataset(_dataset())
    required = set(dataset["coverage"]["required_tools"])
    observed = {str(case["expect"]["tool"]) for case in dataset["cases"]}
    available = {tool.name for tool in delegation_eval_tools(tmp_path, project_dir=tmp_path)}

    assert required == available
    assert required <= observed


def test_task_eval_dataset_covers_lifecycle_regressions():
    cases = load_delegation_eval_cases(_dataset())
    ids = {str(case["id"]) for case in cases}

    assert {
        "list_delegations",
        "delete_delegation_from_recent_list",
        "delete_watch_from_recent_list",
        "hermes_connector_liveness_split",
        "hermes_provider_runtime_drift",
        "openclaw_memory_instruction_injection",
        "openclaw_broad_permission_skill_install",
        "openclaw_background_activity_guard",
        "exact_evening_watch",
        "approve_code",
        "reject_code",
    } <= ids


def test_task_eval_case_matcher_reports_arg_drift():
    case = {
        "id": "delete_delegation_from_recent_list",
        "expect": {
            "tool": "delegate.delete",
            "permission": "write",
            "args": {"run_id": "expected"},
        },
    }
    decision = ModelSyscall(tool="delegate.delete", permission="write", args={"run_id": "actual"})

    errors = match_delegation_eval_case(case, decision)

    assert "args.run_id expected 'expected', got 'actual'" in errors


@pytest.mark.asyncio
async def test_mock_planner_passes_delegation_eval_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("NAVI_MODEL", "mock")

    results = await run_delegation_eval_dataset(
        home=tmp_path,
        project_dir=tmp_path,
        dataset=_dataset(),
        timeout_seconds=1,
    )

    failures = [result for result in results if not result.ok]
    assert failures == []
    assert len(results) >= 38


def test_daily_journey_eval_dataset_is_user_facing():
    dataset = load_daily_journey_eval_dataset(_daily_dataset())
    ids = {str(journey["id"]) for journey in dataset["journeys"]}

    assert {
        "casual_chat_does_not_create_task",
        "local_work_request_gets_one_approval_task",
        "approval_then_background_execution_completes_goal",
        "recurring_evening_lesson_creates_watch",
        "vague_reminder_asks_clarification",
        "user_can_ask_current_task_list",
        "user_can_ask_why_task_not_executed",
        "user_can_reject_pending_task",
        "user_can_clean_failed_tasks",
        "user_can_check_model_provider_status",
        "user_can_check_connector_status",
    } <= ids
    assert all("user_goal" in journey for journey in dataset["journeys"])
    assert len(dataset["journeys"]) >= 11


@pytest.mark.asyncio
async def test_mock_runtime_passes_daily_journey_eval_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("NAVI_MODEL", "mock")
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")

    results = await run_daily_journey_eval_dataset(
        home=tmp_path,
        project_dir=tmp_path,
        dataset=_daily_dataset(),
        timeout_seconds=5,
    )

    failures = [result for result in results if not result.ok]
    assert failures == []


def test_claw_eval_dataset_is_user_task_shaped():
    dataset = load_claw_eval_dataset(_claw_dataset())
    task_ids = {str(task["task_id"]) for task in dataset["tasks"]}
    splits = {str(task["split"]) for task in dataset["tasks"]}

    assert {
        "navi_general_chat_no_task",
        "navi_multi_turn_local_task_approval_execution",
        "navi_general_task_status_query",
        "navi_general_reject_pending_task",
        "navi_general_failed_task_cleanup",
        "navi_general_exact_recurring_watch",
        "navi_general_vague_reminder_clarifies",
        "navi_general_provider_diagnostics",
        "navi_general_connector_diagnostics",
        "navi_safety_broad_permission_skill_install",
    } <= task_ids
    assert {"general", "multi_turn"} <= splits
    assert all("query" in task for task in dataset["tasks"])
    assert all("rubric_dimensions" in task for task in dataset["tasks"])


@pytest.mark.asyncio
async def test_mock_runtime_passes_claw_eval_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("NAVI_MODEL", "mock")
    monkeypatch.setenv("NAVI_EXECUTION_MOCK", "true")

    results = await run_claw_eval_dataset(
        home=tmp_path,
        project_dir=tmp_path,
        dataset=_claw_dataset(),
        attempts=3,
        timeout_seconds=5,
    )

    failures = [result for result in results if not result.ok]
    assert failures == []
    assert all(result.pass_count == 3 for result in results)


def test_weixin_journey_eval_dataset_is_user_visible():
    dataset = load_weixin_journey_eval_dataset(_weixin_dataset())
    ids = {str(journey["id"]) for journey in dataset["journeys"]}

    assert {
        "weixin_hello_replies_and_records_events",
        "weixin_provider_failure_returns_visible_fallback",
        "weixin_local_work_request_gets_approval",
        "weixin_exact_schedule_creates_watch",
        "weixin_vague_reminder_clarifies_without_watch",
        "weixin_duplicate_message_is_ignored",
        "weixin_clean_failed_tasks",
    } <= ids
    assert all("user_goal" in journey for journey in dataset["journeys"])


@pytest.mark.asyncio
async def test_mock_runtime_passes_weixin_journey_eval_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVI_WEIXIN_MOCK", "true")

    results = await run_weixin_journey_eval_dataset(
        home=tmp_path,
        project_dir=tmp_path,
        dataset=_weixin_dataset(),
        timeout_seconds=5,
    )

    failures = [result for result in results if not result.ok]
    assert failures == []
