from __future__ import annotations

from pathlib import Path

from navi.evals import (
    load_task_eval_cases,
    match_task_eval_case,
    task_eval_tools,
    validate_task_eval_cases,
)
from navi.syscalls import ModelSyscall


def _dataset() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "task_cases.yaml"


def test_task_eval_dataset_matches_capability_manifest(tmp_path):
    cases = load_task_eval_cases(_dataset())
    errors = validate_task_eval_cases(cases, task_eval_tools(tmp_path, project_dir=tmp_path))

    assert errors == []


def test_task_eval_dataset_covers_lifecycle_regressions():
    cases = load_task_eval_cases(_dataset())
    ids = {str(case["id"]) for case in cases}

    assert {
        "list_tasks",
        "delete_task_from_recent_list",
        "delete_watch_from_recent_list",
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
