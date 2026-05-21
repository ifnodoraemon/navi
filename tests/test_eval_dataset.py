from __future__ import annotations

from pathlib import Path

from navi.evals import (
    load_task_eval_cases,
    load_task_eval_dataset,
    match_task_eval_case,
    task_eval_tools,
    validate_task_eval_dataset,
)
from navi.syscalls import ModelSyscall


def _dataset() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "task_cases.yaml"


def test_task_eval_dataset_matches_capability_manifest(tmp_path):
    dataset = load_task_eval_dataset(_dataset())
    errors = validate_task_eval_dataset(dataset, task_eval_tools(tmp_path, project_dir=tmp_path))

    assert errors == []


def test_task_eval_dataset_has_100_percent_required_scenario_coverage():
    dataset = load_task_eval_dataset(_dataset())
    required = set(dataset["coverage"]["required_categories"])
    observed = {str(case["category"]) for case in dataset["cases"]}

    assert required <= observed
    assert len(required) >= 18


def test_task_eval_dataset_covers_lifecycle_regressions():
    cases = load_task_eval_cases(_dataset())
    ids = {str(case["id"]) for case in cases}

    assert {
        "list_tasks",
        "delete_task_from_recent_list",
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
        "id": "delete_task_from_recent_list",
        "expect": {
            "tool": "task.delete",
            "permission": "write",
            "args": {"task_id": "expected"},
        },
    }
    decision = ModelSyscall(tool="task.delete", permission="write", args={"task_id": "actual"})

    errors = match_task_eval_case(case, decision)

    assert "args.task_id expected 'expected', got 'actual'" in errors
