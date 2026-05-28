from __future__ import annotations

from pathlib import Path

import pytest

from navi.evals import (
    load_delegation_eval_cases,
    load_delegation_eval_dataset,
    match_delegation_eval_case,
    delegation_eval_tools,
    run_delegation_eval_dataset,
    validate_delegation_eval_dataset,
)
from navi.syscalls import ModelSyscall


def _dataset() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "delegation_cases.yaml"


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
